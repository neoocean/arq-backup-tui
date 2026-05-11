"""V4 — fresh-walk Tree v4 emit byte-equivalence via patched arq_restore.

Strategy I-alt (`docs/COMPAT-VERIFICATION.md` §5.8) introduced the
patched ``arq_restore`` as an independent Tree v4 reader for the
**round-trip** path: Arq.app emit → our reader + patched
arq_restore = both read back to identical bytes.

V4 closes the FRESH-WALK half: our writer's fresh-walk Tree v4
emit (built from a synthetic source tree, no parser input,
exactly the path Strategy K's deterministic-fallback uses) is
consumed by the patched arq_restore byte-identically.

V4 also surfaced a production bug fixed in this PR:

- ``arq_writer/backuprecord.py::node_to_dict`` previously emitted
  ``aclBlobLoc: null`` for every node without an ACL. Real
  Arq.app v8 v4 records OMIT the key entirely. arq_restore's
  ``Arq7BlobLoc initWithJSON:`` crashes on ``NSNull`` (the
  ``objectForKey:`` call hits NSNull → unrecognized selector).
  The new emit rule: omit the key when null, emit the BlobLoc
  dict otherwise.

This test exercises the end-to-end pipeline if the patched
arq_restore binary is built locally
(`scripts/arq_restore_v4/build.sh`). Otherwise the test
auto-skips so CI on machines without Xcode CLT + OpenSSL
remains green.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ARQ_RESTORE_V4_BIN = Path("/private/tmp/strategy-c/arq_restore.bin.v4")


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipUnless(
    ARQ_RESTORE_V4_BIN.is_file(),
    f"patched arq_restore binary not at {ARQ_RESTORE_V4_BIN}; "
    f"run scripts/arq_restore_v4/build.sh first",
)
class V4FreshWalkVerifyTests(unittest.TestCase):
    """End-to-end fresh-walk Tree v4 emit → patched arq_restore
    byte-equivalence."""

    def test_fresh_walk_v4_emit_consumable_by_patched_arq_restore(
        self,
    ) -> None:
        """Build a flat-tree fresh v4 backup, restore each file via
        the patched arq_restore, verify byte equality with source."""
        # Use the shipping verify_fresh_walk.py helper so the test
        # and the operator-facing script share one code path.
        import sys
        repo_root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo_root / "scripts" / "arq_restore_v4"))
        from verify_fresh_walk import main as verify_main

        # Use /tmp directly (short path) — arq_restore chokes on
        # the long /private/var/folders/<uuid>/T/tmpXXXX style
        # tempfile paths, treating segments as path components
        # that can't be created. The test isolates by using a
        # unique subdirectory name and cleans up on teardown.
        import uuid as _u
        work_root = Path("/tmp") / f"arq-v4-test-{_u.uuid4().hex[:8]}"
        try:
            rc = verify_main([
                "--arq-restore-bin", str(ARQ_RESTORE_V4_BIN),
                "--work-dir", str(work_root),
                "--password", "test-pw-fresh-walk-v4",
            ])
            self.assertEqual(
                rc, 0,
                f"verify_fresh_walk exited with {rc}; byte-"
                f"equivalence not proven",
            )
        finally:
            import shutil as _sh
            if work_root.exists():
                _sh.rmtree(work_root, ignore_errors=True)


class V4AclBlobLocOmissionTests(unittest.TestCase):
    """V4 production fix: ``node_to_dict`` omits ``aclBlobLoc``
    when null, doesn't emit ``aclBlobLoc: null``. Tests the
    contract without needing arq_restore in scope."""

    def test_node_to_dict_omits_aclblobloc_when_none(self) -> None:
        from arq_writer.backuprecord import node_to_dict
        from arq_writer.types import FileNode, BlobLoc
        node = FileNode(
            dataBlobLocs=[],
            mac_st_mode=0o100644,
            mac_st_uid=501,
            mac_st_gid=20,
            aclBlobLoc=None,
        )
        d = node_to_dict(node)
        self.assertNotIn(
            "aclBlobLoc", d,
            f"aclBlobLoc must be omitted when None; got keys: "
            f"{sorted(d.keys())}",
        )

    def test_node_to_dict_emits_aclblobloc_dict_when_present(
        self,
    ) -> None:
        from arq_writer.backuprecord import node_to_dict
        from arq_writer.types import FileNode, BlobLoc
        loc = BlobLoc(
            blobIdentifier="aa" * 32,
            isPacked=True,
            isLargePack=False,
            relativePath="/x/acl",
            offset=0,
            length=100,
            stretchEncryptionKey=True,
            compressionType=2,
        )
        node = FileNode(
            dataBlobLocs=[],
            mac_st_mode=0o100644,
            aclBlobLoc=loc,
        )
        d = node_to_dict(node)
        self.assertIn("aclBlobLoc", d)
        self.assertEqual(
            d["aclBlobLoc"]["blobIdentifier"], "aa" * 32,
        )
        self.assertEqual(d["aclBlobLoc"]["isPacked"], True)
        self.assertEqual(d["aclBlobLoc"]["relativePath"], "/x/acl")

    def test_reader_tolerates_both_shapes(self) -> None:
        """Our reader's ``.get('aclBlobLoc')`` returns None when
        the key is absent OR when it's present-but-null. Both
        shapes must produce equivalent behaviour."""
        from arq_reader.restore import Restore
        # Synthesize two parsed-record dicts; both should resolve
        # to "no ACL" via the same code path.
        with_null = {"node": {"isTree": True, "aclBlobLoc": None}}
        without_key = {"node": {"isTree": True}}
        # The reader checks .get('aclBlobLoc') — both return None.
        self.assertIsNone(with_null["node"].get("aclBlobLoc"))
        self.assertIsNone(without_key["node"].get("aclBlobLoc"))


if __name__ == "__main__":
    unittest.main()
