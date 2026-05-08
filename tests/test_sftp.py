"""Lightweight tests for the SFTP backend.

The sandbox doesn't have an SSH server, so we don't drive a real
connection. Instead, the tests cover:

- Construction validation (rejects empty host).
- Spec parsing for ``user@host[:port]:/root``.
- Backend protocol shape (every required method exists).
- Lazy connection: building a backend does no I/O, so the tests
  pass even on a host without ``ssh`` installed.

Real-network coverage will land when a CI environment with an SFTP
server is available; the parsers + state machine here are the
parts that benefit most from unit tests.
"""

from __future__ import annotations

import unittest

from arq_validator import SftpBackend
from arq_validator.cli import _parse_sftp_spec


class SftpBackendConstructionTests(unittest.TestCase):
    def test_empty_host_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SftpBackend(host="")

    def test_construction_does_no_io(self) -> None:
        # Should succeed even without ssh on PATH; no connection
        # attempted until __enter__ is called.
        b = SftpBackend(
            host="nonexistent.invalid", port=22, user="user",
            password="x",
        )
        self.assertEqual(b.host, "nonexistent.invalid")
        self.assertEqual(b.port, 22)
        self.assertEqual(b.user, "user")
        self.assertIsNone(b._master)

    def test_protocol_methods_exist(self) -> None:
        b = SftpBackend(host="h", user="u", password="p")
        for name in (
            "list_dir", "stat_size", "read_range", "read_all",
            "exists", "is_dir",
        ):
            self.assertTrue(hasattr(b, name), f"missing method: {name}")

    def test_calling_method_before_enter_raises(self) -> None:
        from arq_validator.sftp import SftpConnectionError
        b = SftpBackend(host="h", user="u", password="p")
        with self.assertRaises(SftpConnectionError):
            b.list_dir("/")


class SftpSpecParseTests(unittest.TestCase):
    def test_user_host_root(self) -> None:
        u, h, p, r = _parse_sftp_spec("user@example.com:/home/x")
        self.assertEqual((u, h, p, r), ("user", "example.com", 22, "/home/x"))

    def test_user_host_port_root(self) -> None:
        u, h, p, r = _parse_sftp_spec("u@example.com:2222:/srv/backup")
        self.assertEqual((u, h, p, r), ("u", "example.com", 2222, "/srv/backup"))

    def test_missing_at_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_sftp_spec("example.com:/home/x")

    def test_missing_root_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_sftp_spec("u@example.com")

    def test_relative_root_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_sftp_spec("u@example.com:home/x")


if __name__ == "__main__":
    unittest.main()
