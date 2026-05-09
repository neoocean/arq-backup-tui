"""Tests for the SFTP master auto-reconnect on idle drop.

Background — see ``docs/SFTP-THROTTLE-MEASUREMENT.md``. The
"Connection closed by remote host" errors operators were seeing
during long SFTP walks turned out to be stale-channel idle drops
on Hetzner's side, not rate-limit hits. The right fix is to
tear down + re-open the ControlMaster + retry the failing
command immediately, NOT to wait several seconds of exponential
backoff for an entirely-recoverable transient state.

These tests:

- Pin the :data:`_RECONNECT_PATTERNS` invariant (proper subset of
  :data:`_RATE_LIMIT_PATTERNS`, covers the four idle-drop
  signatures we've observed).
- Drive ``_run_with_backoff`` through a fake invoker that simulates
  one reconnect-eligible failure followed by success — proves the
  reconnect path is taken + the retry runs without an exponential
  backoff sleep.
- Confirm the reconnect path is used at most once per call so a
  permanently-dead master can't trap us in an infinite loop.
- Confirm a non-reconnect-eligible rate-limit pattern (e.g.
  "Connection refused") still goes through the regular backoff
  loop — i.e. we didn't accidentally rewire the rate-limit path.

No real network — every test uses a fake invoker that returns
hand-built ``CompletedProcess`` objects.
"""

from __future__ import annotations

import subprocess
import unittest
from typing import List

from arq_validator.sftp import (
    SftpBackend,
    _RATE_LIMIT_PATTERNS,
    _RECONNECT_PATTERNS,
)


class ReconnectPatternsInvariantTests(unittest.TestCase):

    def test_reconnect_patterns_are_subset_of_rate_limit(self) -> None:
        # Every reconnect pattern must also be recognized as a
        # rate-limit pattern; otherwise the wrapper's "matched is
        # None → return" branch would short-circuit before the
        # reconnect-eligibility check ever fires.
        self.assertTrue(
            set(_RECONNECT_PATTERNS).issubset(set(_RATE_LIMIT_PATTERNS)),
            f"_RECONNECT_PATTERNS {set(_RECONNECT_PATTERNS) - set(_RATE_LIMIT_PATTERNS)} "
            f"are not in _RATE_LIMIT_PATTERNS — wrapper would "
            f"short-circuit before considering reconnect.",
        )

    def test_reconnect_set_covers_known_idle_drop_signatures(self) -> None:
        # Sanity that the set hasn't drifted away from the four
        # signatures the throttle measurement identified as
        # idle-drop indicators.
        for pat in (
            "Connection closed by remote host",
            "mux_client_request_session",
            "no master connection",
            "Control socket connect",
        ):
            self.assertIn(pat, _RECONNECT_PATTERNS)


class ReopenMasterRetryTests(unittest.TestCase):
    """Drive ``_run_with_backoff`` through a fake invoker that
    simulates idle drops + recoveries."""

    def _make_backend(self):
        # Real SftpBackend instance, but we never call __enter__ /
        # the network — we only exercise _run_with_backoff with a
        # synthetic invoker. The reopen path is patched to a noop
        # that just returns True (so we don't try to actually
        # respawn ssh).
        backend = SftpBackend(
            host="example.invalid",
            port=22, user="x",
            password="placeholder",
            backoff_base_sec=0.001,
            backoff_max_sec=0.01,
            backoff_max_attempts=4,
        )
        # Stub the reconnect to record calls + always succeed.
        backend._reopen_count = 0  # type: ignore[attr-defined]

        def _fake_reopen():
            backend._reopen_count += 1  # type: ignore[attr-defined]
            return True

        backend._reopen_master = _fake_reopen  # type: ignore[assignment]
        return backend

    def test_idle_drop_triggers_reconnect_and_retries_immediately(self) -> None:
        backend = self._make_backend()
        events = []
        backend._backoff_callback = (
            lambda kind, payload: events.append((kind, payload))
        )

        attempts = []

        def invoker():
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                return subprocess.CompletedProcess(
                    args=[], returncode=1, stdout=b"",
                    stderr=b"Connection closed by remote host\n",
                )
            # After the reconnect, succeed.
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=b"ok", stderr=b"",
            )

        cp = backend._run_with_backoff(
            "sftp", invoker, command_summary="ls /",
        )
        self.assertEqual(cp.returncode, 0)
        # Two invoker calls: original + post-reconnect retry.
        self.assertEqual(len(attempts), 2)
        # Reconnect was used exactly once.
        self.assertEqual(backend._reopen_count, 1)
        # ssh_master_reconnect event surfaced (operators see it
        # in the TUI as "Reconnecting SFTP master…").
        self.assertIn(
            "ssh_master_reconnect",
            [k for k, _ in events],
        )
        # We should NOT have emitted a rate_limit_backoff (the
        # whole point of the reconnect fast path is to skip the
        # exponential sleep).
        self.assertNotIn(
            "rate_limit_backoff",
            [k for k, _ in events],
        )

    def test_reconnect_used_at_most_once_per_call(self) -> None:
        backend = self._make_backend()
        backend._backoff_callback = lambda *a, **kw: None

        def invoker():
            # Always returns the same idle-drop stderr so
            # reconnect would help, but only once.
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"",
                stderr=b"Connection closed by remote host\n",
            )

        # Suppress the threshold raise so the test sees the full
        # loop; with max_attempts=4 + threshold disabled the
        # wrapper falls through to the last-cp return.
        backend._rate_limit.threshold = 99
        cp = backend._run_with_backoff(
            "sftp", invoker, command_summary="ls /",
        )
        # Reconnect was tried exactly once even though every
        # attempt's stderr matched the reconnect pattern.
        self.assertEqual(backend._reopen_count, 1)
        self.assertEqual(cp.returncode, 1)

    def test_non_reconnect_rate_limit_still_uses_backoff(self) -> None:
        """A "Connection refused" stderr is in
        :data:`_RATE_LIMIT_PATTERNS` but NOT in
        :data:`_RECONNECT_PATTERNS` (it means the master never
        established, not that an existing one died). Must use
        regular exponential backoff, not the reconnect fast path."""
        backend = self._make_backend()
        events = []
        backend._backoff_callback = (
            lambda kind, payload: events.append((kind, payload))
        )

        def invoker():
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"",
                stderr=b"ssh: Connection refused\n",
            )

        backend._rate_limit.threshold = 99
        backend._run_with_backoff(
            "sftp", invoker, command_summary="ls /",
        )
        # No reconnect should have fired.
        self.assertEqual(backend._reopen_count, 0)
        self.assertNotIn(
            "ssh_master_reconnect",
            [k for k, _ in events],
        )
        # Backoff sleeps DID fire (every attempt > 0 has one).
        self.assertGreater(
            sum(1 for k, _ in events if k == "rate_limit_backoff"),
            0,
        )

    def test_failed_reopen_falls_through_to_backoff(self) -> None:
        """If `_reopen_master()` returns False the wrapper must
        fall through to the regular exponential-backoff loop
        rather than treating it as success."""
        backend = self._make_backend()
        # Override the stub to fail.
        backend._reopen_master = lambda: False  # type: ignore[assignment]

        events = []
        backend._backoff_callback = (
            lambda kind, payload: events.append((kind, payload))
        )

        def invoker():
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"",
                stderr=b"Connection closed by remote host\n",
            )

        backend._rate_limit.threshold = 99
        backend._run_with_backoff(
            "sftp", invoker, command_summary="ls /",
        )
        # Reconnect was attempted (notify fires before the
        # boolean check), but since it failed we should still
        # see backoff sleeps from later attempts.
        self.assertIn(
            "ssh_master_reconnect",
            [k for k, _ in events],
        )
        self.assertGreater(
            sum(1 for k, _ in events if k == "rate_limit_backoff"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
