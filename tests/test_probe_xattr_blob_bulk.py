"""Tests for ``scripts/probe_xattr_blob_bulk.py``.

The probe walks a real backup destination and decodes every xattr
blob it sees, so its happy path can't be exercised in CI. What
*can* be exercised — and what regressed twice now — is the
shape-tolerant ``_fetch_blob`` and the ``--local-root`` CLI
plumbing that lets the probe target a :class:`LocalBackend`
instead of an SFTP one.

Both regressions surfaced from real-data runs:

- ``_fetch_blob`` originally used ``getattr(blob_loc, ...)`` only,
  which silently returned ``""`` for ``dict``-shaped BlobLocs (the
  shape that ``parse_backuprecord`` emits for root-level xattrs).
  Every root xattr blob was then treated as a fetch failure even
  though the data on disk was perfectly valid.
- ``--local-root`` was added so operators with a locally-mounted
  destination don't have to round-trip through SFTP, which made
  bulk runs (max-walk ≥1000) infeasible on slower SSH links.

These tests pin both behaviours so a future refactor can't quietly
strip them.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# The probe is a top-level script, not a package module, so import
# it by path rather than by ``import scripts.probe_xattr_blob_bulk``
# (the ``scripts/`` directory is intentionally outside the
# ``arq_*`` namespace and not a Python package).
_PROBE_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "probe_xattr_blob_bulk.py"
)
_spec = importlib.util.spec_from_file_location(
    "probe_xattr_blob_bulk", _PROBE_PATH,
)
probe = importlib.util.module_from_spec(_spec)
sys.modules["probe_xattr_blob_bulk"] = probe
assert _spec.loader is not None
_spec.loader.exec_module(probe)


class _RecordingBackend:
    """Minimal backend stub that records calls and serves canned bytes.

    Sufficient for ``_fetch_blob`` exercises — the real backends
    (LocalBackend, SftpBackend) only need to satisfy
    ``read_all`` / ``read_range`` for this code path.
    """

    def __init__(self, payload: bytes = b"PAYLOAD") -> None:
        self.payload = payload
        self.calls: list = []

    def read_all(self, path: str) -> bytes:
        self.calls.append(("read_all", path))
        return self.payload

    def read_range(
        self, path: str, offset: int, length: int,
    ) -> bytes:
        self.calls.append(("read_range", path, offset, length))
        return self.payload[offset : offset + length]


class FetchBlobShapeTests(unittest.TestCase):
    """``_fetch_blob`` accepts both shapes the walker collects.

    Pre-fix the dict shape silently returned ``b""`` because
    ``getattr(dict_obj, "relativePath", "")`` always falls through
    to the default. The fix branches on ``isinstance(..., dict)``
    so root xattrs and tree-walked xattrs both round-trip.
    """

    def test_dataclass_packed_reads_via_range(self) -> None:
        from arq_writer.types import BlobLoc

        loc = BlobLoc(
            blobIdentifier="abc",
            relativePath="/p/blobpacks/00.pack",
            offset=4, length=6, isPacked=True,
        )
        be = _RecordingBackend(b"AAAATARGETBB")
        out = probe._fetch_blob(be, loc)
        self.assertEqual(out, b"TARGET")
        self.assertEqual(
            be.calls,
            [("read_range", "/p/blobpacks/00.pack", 4, 6)],
        )

    def test_dataclass_nonpacked_reads_via_read_all(self) -> None:
        from arq_writer.types import BlobLoc

        loc = BlobLoc(
            blobIdentifier="abc",
            relativePath="/p/standardobjects/x",
            isPacked=False,
        )
        be = _RecordingBackend(b"WHOLE")
        self.assertEqual(probe._fetch_blob(be, loc), b"WHOLE")
        self.assertEqual(
            be.calls, [("read_all", "/p/standardobjects/x")],
        )

    def test_dict_packed_reads_via_range(self) -> None:
        # The shape that parse_backuprecord emits for root-level
        # xattrs: a plain dict mirroring the BlobLoc fields.
        loc = {
            "blobIdentifier": "abc",
            "relativePath": "/p/blobpacks/00.pack",
            "offset": 4, "length": 6, "isPacked": True,
        }
        be = _RecordingBackend(b"AAAATARGETBB")
        out = probe._fetch_blob(be, loc)
        self.assertEqual(out, b"TARGET")
        self.assertEqual(
            be.calls,
            [("read_range", "/p/blobpacks/00.pack", 4, 6)],
        )

    def test_dict_nonpacked_reads_via_read_all(self) -> None:
        loc = {"relativePath": "/p/x", "isPacked": False}
        be = _RecordingBackend(b"WHOLE")
        self.assertEqual(probe._fetch_blob(be, loc), b"WHOLE")
        self.assertEqual(be.calls, [("read_all", "/p/x")])

    def test_empty_rel_short_circuits_for_both_shapes(self) -> None:
        # Empty-relativePath BlobLocs are sentinels for "no xattr
        # blob attached" — the walker collects them (they're in
        # the BlobLoc list) but they have nothing to fetch, so
        # ``_fetch_blob`` returns b"" without hitting the backend.
        from arq_writer.types import BlobLoc

        be = _RecordingBackend(b"NEVER_SERVED")
        self.assertEqual(probe._fetch_blob(be, {}), b"")
        self.assertEqual(
            probe._fetch_blob(be, {"relativePath": ""}), b"",
        )
        self.assertEqual(
            probe._fetch_blob(be, BlobLoc(blobIdentifier="x")), b"",
        )
        # Backend was never called.
        self.assertEqual(be.calls, [])


class LocalRootCliPlumbingTests(unittest.TestCase):
    """``--local-root`` skips SFTP cred resolution and constructs a
    :class:`LocalBackend` instead. We don't drive the full probe
    here (no real Arq destination in CI), but we verify each
    decision branch — invalid path, missing password, empty
    destination — exits with the expected diagnostic so the
    early-failure surface stays stable.
    """

    def test_invalid_local_root_path_exits_clearly(self) -> None:
        with patch(
            "tests.integration._creds.load_dest_password",
            return_value="anything",
        ):
            with self.assertRaises(SystemExit) as cm:
                probe._main([
                    "--local-root",
                    "/this/path/should/not/exist/anywhere",
                ])
        self.assertIn("--local-root invalid", str(cm.exception))

    def test_missing_dest_password_exits_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Force-clear every source the helper might pull from.
            with patch(
                "tests.integration._creds.load_dest_password",
                return_value=None,
            ):
                with self.assertRaises(SystemExit) as cm:
                    probe._main(["--local-root", td])
        self.assertIn(
            "dest_password unavailable", str(cm.exception),
        )

    def test_empty_local_destination_reports_no_subtrees(self) -> None:
        # An empty directory is a valid LocalBackend root but has no
        # ``<computer-uuid>/`` children, so layout discovery yields
        # nothing — that's the smallest reachable code path that
        # confirms LocalBackend was actually constructed and
        # threaded into ``discover_layout``.
        with tempfile.TemporaryDirectory() as td:
            with patch(
                "tests.integration._creds.load_dest_password",
                return_value="anything",
            ):
                with self.assertRaises(SystemExit) as cm:
                    probe._main([
                        "--local-root", td, "--max-walk", "1",
                    ])
        self.assertIn(
            "no computer subtrees", str(cm.exception),
        )


if __name__ == "__main__":
    unittest.main()
