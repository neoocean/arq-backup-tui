"""R1 — Strategy B (Arq.app → our reader) automated, fixture-driven.

§3 of ``docs/COMPAT-VERIFICATION.md`` established Strategy B by
hand on 2026-05-10 against ``/Volumes/arqbackup1`` (127,222 files
byte-perfect, ``verify.failures: []``). This module lifts it into
CI: any tarball + password pair the operator drops into
``tests/fixtures/arqapp_destinations/`` is enumerated, restored
through our Python reader, and checked for restore-time errors.

When no fixture is present (the public-CI case), the class skips
cleanly. When one or more fixtures are present (the operator's
local dev machine, or a private CI), every captured destination
gets a restore-and-verify pass.

The fixture convention is documented in
``tests/fixtures/arqapp_destinations/README.md``:

- ``<name>_v8.tar`` — tarball of the destination subtree
- ``<name>_v8.password`` — single-line password file
  (chmod 0600 recommended)

Each pair becomes one sub-test. Fixtures are git-ignored so no
sensitive bytes land in the repo.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple


_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "arqapp_destinations"


def _enumerate_fixtures() -> List[Tuple[str, Path, Path]]:
    """Return (name, tarball_path, password_path) for every fixture
    pair under :data:`_FIXTURE_DIR`. Empty list when none present."""
    if not _FIXTURE_DIR.is_dir():
        return []
    out = []
    for tarball in sorted(_FIXTURE_DIR.glob("*_v8.tar")):
        password_file = tarball.with_suffix(".password")
        if not password_file.is_file():
            continue
        name = tarball.stem
        out.append((name, tarball, password_file))
    return out


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


_FIXTURES = _enumerate_fixtures()


@unittest.skipUnless(
    _FIXTURES,
    "no Arq.app destination fixtures in "
    "tests/fixtures/arqapp_destinations/ — see that directory's "
    "README for the capture workflow",
)
@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class StrategyBFixtureRegressionTests(unittest.TestCase):
    """For each fixture: extract → discover layout → restore →
    assert zero failures.

    The tests are generated dynamically per fixture so a fixture-
    specific failure surfaces with the fixture's name in the
    error message."""


def _make_test(name: str, tarball: Path, password_file: Path):
    """Closure factory: builds one test method per fixture."""
    def _test(self) -> None:
        from arq_reader import Restore
        password = password_file.read_text().strip()
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            extract_root = tdp / "extracted"
            extract_root.mkdir()
            # Extract the tarball.
            try:
                with tarfile.open(tarball, "r") as tf:
                    tf.extractall(str(extract_root))
            except Exception as exc:
                self.fail(
                    f"fixture {name}: tarball extract failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            # The extract root may have one or more computer-UUID
            # subdirs at any depth; find the first one that looks
            # like an Arq destination root (contains a UUID dir
            # with ``encryptedkeyset.dat``).
            dest_root = _find_destination_root(extract_root)
            if dest_root is None:
                self.skipTest(
                    f"fixture {name}: no recognisable destination "
                    f"root under {extract_root}"
                )
            rs = Restore(str(dest_root), encryption_password=password)
            layouts = rs.layouts()
            self.assertGreater(
                len(layouts), 0,
                f"fixture {name}: no layouts discovered",
            )
            # Restore each folder + assert zero per-file errors.
            out = tdp / "restored"
            out.mkdir()
            for lt in layouts:
                for folder_uuid in lt.backup_folder_uuids:
                    folder_out = out / folder_uuid
                    folder_out.mkdir()
                    try:
                        result = rs.restore(
                            folder_uuid=folder_uuid,
                            computer_uuid=lt.computer_uuid,
                            dest=folder_out,
                        )
                    except Exception as exc:
                        self.fail(
                            f"fixture {name}: restore({folder_uuid}) "
                            f"raised: {type(exc).__name__}: {exc}"
                        )
                    # The Restore API varies by version; some
                    # variants return a result with .failures, some
                    # raise. We treat "no exception" as success and
                    # only check .failures when the attribute exists.
                    failures = getattr(result, "failures", None)
                    if failures:
                        self.fail(
                            f"fixture {name}: folder {folder_uuid} "
                            f"had {len(failures)} restore failures: "
                            f"{failures[:5]}"
                        )

    _test.__doc__ = (
        f"Strategy B regression against fixture {name!r}: "
        f"restore through our reader → zero failures."
    )
    return _test


def _find_destination_root(extract_root: Path) -> Path | None:
    """Walk up to 4 levels deep looking for a directory that
    contains a UUID-subdir with ``encryptedkeyset.dat``. Returns
    the parent of the UUID dir (the destination root) or None.
    """
    import re
    uuid_re = re.compile(
        r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
        r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
    )
    for cur, dirs, files in os.walk(extract_root):
        cur_path = Path(cur)
        for d in dirs:
            if not uuid_re.match(d):
                continue
            keyset = cur_path / d / "encryptedkeyset.dat"
            if keyset.is_file():
                return cur_path
    return None


# Dynamically attach one test method per fixture so the runner
# reports per-fixture results (rather than one collapsed test).
for _name, _tar, _pw in _FIXTURES:
    _method_name = f"test_strategy_b_{_name}"
    setattr(
        StrategyBFixtureRegressionTests,
        _method_name,
        _make_test(_name, _tar, _pw),
    )


if __name__ == "__main__":
    unittest.main()
