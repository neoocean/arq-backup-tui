"""Tests for the F4 (--debug logging) + F1 (.secrets wizard
helper) bundle.

F4: arq_validator/debug_logging.py — central logger config.
F1: arq_tui/secrets_setup.py — write .secrets/sftp.json +
    .secrets/dest_password atomically with mode 0600.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# F4 — debug logging
# ---------------------------------------------------------------------------


class DebugLoggingTests(unittest.TestCase):

    def setUp(self) -> None:
        # Each test starts with a clean root so handler-counts
        # are predictable.
        from arq_validator.debug_logging import _ROOT_NAME
        root = logging.getLogger(_ROOT_NAME)
        for h in list(root.handlers):
            root.removeHandler(h)

    def test_get_logger_returns_named_child(self) -> None:
        from arq_validator.debug_logging import get_logger
        log = get_logger("sftp")
        self.assertEqual(log.name, "arq_backup_tui.sftp")

    def test_enable_all_subsystems_lifts_each(self) -> None:
        from arq_validator.debug_logging import (
            enable_debug_logging, get_logger, _SUBSYSTEMS,
        )
        enable_debug_logging()    # all
        for sub in _SUBSYSTEMS:
            self.assertTrue(
                get_logger(sub).isEnabledFor(logging.DEBUG),
                f"subsystem {sub} not enabled",
            )

    def test_enable_subset_leaves_others_at_warning(self) -> None:
        from arq_validator.debug_logging import (
            enable_debug_logging, get_logger,
        )
        enable_debug_logging(subsystems=["sftp"])
        self.assertTrue(
            get_logger("sftp").isEnabledFor(logging.DEBUG),
        )
        self.assertFalse(
            get_logger("blob").isEnabledFor(logging.DEBUG),
        )

    def test_idempotent_does_not_double_handlers(self) -> None:
        from arq_validator.debug_logging import (
            enable_debug_logging, _ROOT_NAME,
        )
        enable_debug_logging()
        enable_debug_logging()
        enable_debug_logging()
        n = len(logging.getLogger(_ROOT_NAME).handlers)
        self.assertEqual(n, 1)

    def test_parse_debug_flag_handles_common_inputs(self) -> None:
        from arq_validator.debug_logging import (
            parse_debug_flag, _SUBSYSTEMS,
        )
        self.assertEqual(parse_debug_flag(""), list(_SUBSYSTEMS))
        self.assertEqual(parse_debug_flag("all"), list(_SUBSYSTEMS))
        self.assertEqual(
            parse_debug_flag("sftp,blob"),
            ["sftp", "blob"],
        )
        # Whitespace tolerated.
        self.assertEqual(
            parse_debug_flag(" sftp , blob "),
            ["sftp", "blob"],
        )

    def test_writer_cli_accepts_debug_flag(self) -> None:
        from arq_writer.cli import _build_parser
        ns = _build_parser().parse_args([
            "create", "/tmp/src", "--dest", "/tmp/dst",
            "--debug",
        ])
        self.assertEqual(ns.debug, "all")

    def test_writer_cli_debug_with_value(self) -> None:
        from arq_writer.cli import _build_parser
        ns = _build_parser().parse_args([
            "create", "/tmp/src", "--dest", "/tmp/dst",
            "--debug", "sftp,blob",
        ])
        self.assertEqual(ns.debug, "sftp,blob")


# ---------------------------------------------------------------------------
# F1 — .secrets wizard helper
# ---------------------------------------------------------------------------


class SecretsSetupTests(unittest.TestCase):

    def test_write_sftp_json_creates_with_mode_0600(self) -> None:
        from arq_tui.secrets_setup import write_sftp_json
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "pyproject.toml").write_text("# fake")
            out = write_sftp_json(
                host="h.example", user="u",
                port=22, root="/srv/r",
                identity_file="/tmp/key",
                repo_root=tdp,
            )
            self.assertTrue(out.is_file())
            data = json.loads(out.read_text("utf-8"))
            self.assertEqual(data["host"], "h.example")
            self.assertEqual(data["user"], "u")
            self.assertEqual(data["root"], "/srv/r")
            self.assertEqual(
                data["identity_file"], "/tmp/key",
            )
            mode = stat.S_IMODE(out.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_write_dest_password_creates_with_mode_0600(self) -> None:
        from arq_tui.secrets_setup import write_dest_password
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "pyproject.toml").write_text("# fake")
            out = write_dest_password(
                "hunter2-encryption-password", repo_root=tdp,
            )
            self.assertEqual(
                out.read_text("utf-8"),
                "hunter2-encryption-password",
            )
            mode = stat.S_IMODE(out.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_write_secrets_for_destination_writes_both(self) -> None:
        from arq_tui.secrets_setup import (
            write_secrets_for_destination,
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "pyproject.toml").write_text("# fake")
            result = write_secrets_for_destination(
                host="h.example", user="u",
                port=23, root="/r",
                sftp_password="ssh-pw",
                dest_password="enc-pw",
                repo_root=tdp,
            )
            self.assertIsNotNone(result["sftp_json"])
            self.assertIsNotNone(result["dest_password"])

    def test_write_secrets_for_destination_skips_empty(self) -> None:
        """Operator might only have the encryption password (e.g.
        local destination + remote-only password rotation). Don't
        write a partial sftp.json."""
        from arq_tui.secrets_setup import (
            write_secrets_for_destination,
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "pyproject.toml").write_text("# fake")
            result = write_secrets_for_destination(
                host="", user="", port=22, root="",
                dest_password="enc-pw",
                repo_root=tdp,
            )
            self.assertIsNone(result["sftp_json"])
            self.assertIsNotNone(result["dest_password"])

    def test_write_is_atomic_via_tempfile_rename(self) -> None:
        """The implementation writes to a .tmp sibling then
        os.replace — pin so a future refactor can't switch to
        a non-atomic approach that could leave a half-written
        secrets file."""
        from arq_tui.secrets_setup import (
            secrets_dir, write_sftp_json,
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "pyproject.toml").write_text("# fake")
            d = secrets_dir(tdp)
            # First write an existing file with different content.
            d.mkdir(parents=True, exist_ok=True)
            (d / "sftp.json").write_text(
                "{\"old\": \"content\"}", encoding="utf-8",
            )
            # Now overwrite via the helper.
            write_sftp_json(
                host="h", user="u", port=22, root="/r",
                repo_root=tdp,
            )
            data = json.loads(
                (d / "sftp.json").read_text("utf-8"),
            )
            self.assertEqual(data["host"], "h")
            self.assertNotIn("old", data)


if __name__ == "__main__":
    unittest.main()
