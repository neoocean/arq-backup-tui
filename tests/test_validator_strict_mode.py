"""R2 — Validator strict mode round-trip byte equivalence.

These tests pin the strict-mode wiring (``check_arq7_compatibility(
... strict=True)``) against destinations the writer produced. The
property being asserted matches §5.6:

    For every parseable on-disk artefact (backuprecord, tree
    binary, xattr blob), ``parse → serialize`` is byte-identical
    to the source.

If a future serialize-layer refactor regresses any of those three,
the strict-mode CheckResult flips to ``passed=False``. The schema
checker (default mode) can't catch that — it would just see the
re-parsed dict has the right shape.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from arq_validator import LocalBackend, check_arq7_compatibility


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
class StrictModeRoundTripTests(unittest.TestCase):
    """End-to-end: build a tiny real backup, check it with
    strict=True, expect every RT* CheckResult to report zero
    drift."""

    def _build_tiny_backup(self, td: Path) -> str:
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"alpha")
        (src / "b.txt").write_bytes(b"bravo")
        (src / "subdir").mkdir()
        (src / "subdir" / "c.txt").write_bytes(b"charlie")
        dest = td / "dest"
        build_backup(
            str(src), str(dest),
            encryption_password="pw", backup_name="strict-rt",
        )
        return str(dest)

    def test_strict_mode_finds_no_drift_on_writer_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest = self._build_tiny_backup(tdp)
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw", strict=True,
            )
            rt_checks = [
                c for c in report.checks if c.id.startswith("RT")
            ]
            self.assertGreater(
                len(rt_checks), 0,
                "strict mode should have produced at least one RT* result",
            )
            # No RT* failures.
            rt_failures = [c for c in rt_checks if not c.passed]
            self.assertEqual(
                rt_failures, [],
                "strict mode round-trip should be drift-free on "
                "writer's own output; failures: "
                + ", ".join(
                    f"{c.id} {c.name}: {c.message}"
                    for c in rt_failures
                ),
            )

    def test_strict_mode_off_by_default(self) -> None:
        # The default invocation (strict=False) should produce no
        # RT* entries — they're opt-in.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest = self._build_tiny_backup(tdp)
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
            )
            rt_checks = [
                c for c in report.checks if c.id.startswith("RT")
            ]
            self.assertEqual(rt_checks, [])

    def test_strict_mode_sample_cap_zero_walks_nothing(self) -> None:
        # sample_cap=0 should bound the standardobjects sweep to
        # zero iterations. RT1 (backuprecord) still runs because
        # backuprecord traversal is not subject to the same cap.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest = self._build_tiny_backup(tdp)
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                strict=True, strict_sample_cap=0,
            )
            rt2 = [c for c in report.checks if c.id == "RT2"]
            # With cap=0 we expect the "no parseable blobs sampled"
            # success result (or zero walked + zero drift on the
            # summary line).
            self.assertGreater(len(rt2), 0)
            for c in rt2:
                self.assertTrue(c.passed)

    def test_strict_mode_detects_injected_tree_drift(self) -> None:
        """If we corrupt a tree blob's plaintext by one byte before
        re-encrypting it back into the destination, RT2 should
        catch the drift on the next strict-mode sweep.

        This exercises the failure path; the prior test exercised
        the success path. Together they pin both directions of the
        strict-mode contract.
        """
        # The cleanest way to inject drift is to write a synthetic
        # tree blob with a deliberately non-round-trip-stable
        # input — but our serialize layer guarantees round-trip on
        # everything it can parse. So instead we patch
        # ``write_tree`` for the duration of a single strict-mode
        # sweep and verify the validator flags the resulting drift.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest = self._build_tiny_backup(tdp)
            backend = LocalBackend(dest)
            from unittest import mock
            import arq_writer.serialize as serialize_mod
            real_write_tree = serialize_mod.write_tree

            def _drift_write_tree(tree, *, version=4):
                # Emit one extra zero byte at the end → guaranteed
                # bytes-differ vs source.
                return real_write_tree(tree, version=version) + b"\x00"

            with mock.patch(
                "arq_writer.serialize.write_tree",
                side_effect=_drift_write_tree,
            ):
                report = check_arq7_compatibility(
                    backend, "/", encryption_password="pw",
                    strict=True,
                )
            rt2_fail = [
                c for c in report.checks
                if c.id == "RT2" and not c.passed
            ]
            self.assertGreater(
                len(rt2_fail), 0,
                "RT2 should have flagged the injected tree drift",
            )


if __name__ == "__main__":
    unittest.main()
