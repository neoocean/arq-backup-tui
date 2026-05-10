"""Tests for ``arq_writer.backup._derive_volume_name``.

GAP-E (HANDOFF.md): the BackupRecord plist's ``volumeName`` field
on Arq.app v8 carries the user-facing volume name of the source
path's volume — sampled 2026-05-10 against ``/Volumes/arqbackup1``
across 9 backup folders, the real values were
``{"ssd2", "ssd", "Macintosh HD", "vault", None}``. Our writer
previously emitted ``""`` always, which dropped a meaningful
metadata bit.

These tests pin the derivation logic without requiring a
particular host's mount setup:

- ``/Volumes/<X>/...`` paths → ``X`` (works on any macOS host
  regardless of which volumes are actually mounted, because the
  helper just splits on path components).
- non-``/Volumes`` paths on macOS → boot volume name (best-effort
  via ``diskutil info``; we mock this so the test is portable).
- non-macOS hosts → ``""``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

from arq_writer.backup import _derive_volume_name


class VolumeNameDerivationTests(unittest.TestCase):

    def test_volumes_path_returns_volume_directory_name(self) -> None:
        # Pure path-component split — works on any host even when
        # ``/Volumes/test-vol`` doesn't exist (the helper resolves
        # symlinks, but that's a no-op for an absolute path with no
        # links). Patch sys.platform to "darwin" so the helper takes
        # the macOS branch.
        with mock.patch("arq_writer.backup.sys") as fake_sys:
            fake_sys.platform = "darwin"
            # Path.resolve() on a non-existent path returns it
            # as-is on Python 3.6+ — relying on that here.
            with mock.patch.object(
                Path, "resolve",
                lambda self, **kw: Path("/Volumes/test-vol/sub/file"),
            ):
                self.assertEqual(
                    _derive_volume_name(Path("/Volumes/test-vol/sub/file")),
                    "test-vol",
                )

    def test_non_volumes_path_consults_diskutil(self) -> None:
        # For paths NOT under /Volumes/, the helper shells out to
        # ``diskutil info`` and parses the ``Volume Name:`` line.
        # We mock subprocess.run to provide a deterministic stdout.
        fake_result = mock.Mock()
        fake_result.returncode = 0
        fake_result.stdout = (
            "   Device Identifier:  disk3s5\n"
            "   Volume Name:        Macintosh HD\n"
            "   Mount Point:        /\n"
        )
        with mock.patch("arq_writer.backup.sys") as fake_sys:
            fake_sys.platform = "darwin"
            with mock.patch.object(
                Path, "resolve",
                lambda self, **kw: Path("/Users/x/somefile"),
            ):
                with mock.patch(
                    "subprocess.run", return_value=fake_result,
                ):
                    self.assertEqual(
                        _derive_volume_name(Path("/Users/x/somefile")),
                        "Macintosh HD",
                    )

    def test_diskutil_failure_falls_back_to_root(self) -> None:
        # If ``diskutil info <path>`` fails (returncode != 0), the
        # helper retries against ``/`` and parses that result.
        first_fail = mock.Mock(returncode=1, stdout="")
        root_ok = mock.Mock(
            returncode=0,
            stdout="   Volume Name:    My Boot Volume\n",
        )
        with mock.patch("arq_writer.backup.sys") as fake_sys:
            fake_sys.platform = "darwin"
            with mock.patch.object(
                Path, "resolve",
                lambda self, **kw: Path("/Users/x/somefile"),
            ):
                with mock.patch(
                    "subprocess.run", side_effect=[first_fail, root_ok],
                ):
                    self.assertEqual(
                        _derive_volume_name(Path("/Users/x/somefile")),
                        "My Boot Volume",
                    )

    def test_diskutil_total_failure_returns_empty_string(self) -> None:
        # Both diskutil calls failing → empty string. The writer
        # must never raise from this helper — degraded metadata is
        # always preferable to a crashed backup.
        all_fail = mock.Mock(returncode=1, stdout="")
        with mock.patch("arq_writer.backup.sys") as fake_sys:
            fake_sys.platform = "darwin"
            with mock.patch.object(
                Path, "resolve",
                lambda self, **kw: Path("/Users/x/somefile"),
            ):
                with mock.patch(
                    "subprocess.run", return_value=all_fail,
                ):
                    self.assertEqual(
                        _derive_volume_name(Path("/Users/x/somefile")),
                        "",
                    )

    def test_diskutil_subprocess_error_returns_empty_string(
        self,
    ) -> None:
        # OSError from subprocess.run (e.g. /usr/sbin/diskutil
        # missing) must also produce ``""`` rather than propagate.
        with mock.patch("arq_writer.backup.sys") as fake_sys:
            fake_sys.platform = "darwin"
            with mock.patch.object(
                Path, "resolve",
                lambda self, **kw: Path("/Users/x/somefile"),
            ):
                with mock.patch(
                    "subprocess.run",
                    side_effect=OSError("no such file"),
                ):
                    self.assertEqual(
                        _derive_volume_name(Path("/Users/x/somefile")),
                        "",
                    )

    def test_non_darwin_returns_empty_string(self) -> None:
        # Linux / Windows / etc. — no equivalent semantic.
        with mock.patch("arq_writer.backup.sys") as fake_sys:
            fake_sys.platform = "linux"
            self.assertEqual(
                _derive_volume_name(Path("/some/path")), "",
            )
        with mock.patch("arq_writer.backup.sys") as fake_sys:
            fake_sys.platform = "win32"
            self.assertEqual(
                _derive_volume_name(Path("C:/Users/foo")), "",
            )

    def test_resolve_failure_returns_empty_string(self) -> None:
        # If Path.resolve() blows up (e.g. permission error walking
        # symlinks), the helper degrades to ``""`` — same defensive
        # stance as the diskutil-failure branches.
        with mock.patch("arq_writer.backup.sys") as fake_sys:
            fake_sys.platform = "darwin"
            with mock.patch.object(
                Path, "resolve",
                side_effect=OSError("permission denied"),
            ):
                self.assertEqual(
                    _derive_volume_name(Path("/some/path")), "",
                )


@unittest.skipUnless(
    sys.platform == "darwin",
    "macOS-specific volume name extraction",
)
class VolumeNameDerivationOnMacOSTests(unittest.TestCase):
    """Real (non-mocked) probe against the running host. Skipped on
    non-macOS so CI on Linux still passes."""

    def test_root_volume_returns_nonempty_name(self) -> None:
        # ``/`` is always mounted on macOS; the boot volume's name
        # is user-customisable but always present.
        name = _derive_volume_name(Path("/"))
        self.assertIsInstance(name, str)
        self.assertTrue(name, "boot volume name should be non-empty")

    def test_volumes_subpath_returns_volume_name(self) -> None:
        # On any macOS host with a mounted /Volumes/<X>, the helper
        # returns X. Pick an existing /Volumes/ entry; if there's
        # no /Volumes/ children at all, skip.
        volumes_dir = Path("/Volumes")
        candidates = [
            p for p in volumes_dir.iterdir()
            if p.is_dir() and not p.is_symlink()
        ] if volumes_dir.is_dir() else []
        if not candidates:
            self.skipTest("no /Volumes/<X> entries available")
        target = candidates[0]
        self.assertEqual(
            _derive_volume_name(target / "any" / "subpath"),
            target.name,
        )


if __name__ == "__main__":
    unittest.main()
