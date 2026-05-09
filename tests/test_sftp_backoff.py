"""Tests for the SFTP backend's adaptive backoff.

Hetzner Storage Box (and most SFTP-only providers with similar
chrooting) silently throttles on too-many-connections-per-minute.
The previous behaviour was: detect ~20 consecutive
"Connection closed by remote host" stderrs and fail-fast with
:class:`SftpRateLimitedError`. The walk crashed mid-flight and
the operator had to retry by hand.

The new behaviour wraps every command in
:meth:`SftpBackend._run_with_backoff` — on a rate-limit pattern
hit we sleep an exponential-with-jitter delay and retry, surfacing
``rate_limit_backoff`` events to the caller's progress callback so
the TUI can show "Throttling for X seconds…" without losing the
run.

These tests:

- Pin the :class:`_BackoffPolicy` curve (no random / no
  ``time.sleep``).
- Drive a real :class:`SftpBackend` through a fake invoker that
  returns rate-limit-shaped stderr for the first N calls and a
  clean success on the (N+1)th — confirming the wrapper retries
  and emits the backoff callback.

Network is never touched.
"""

from __future__ import annotations

import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from arq_validator.sftp import (
    SftpBackend,
    SftpRateLimitedError,
    _BackoffPolicy,
    _RateLimitTracker,
)


class BackoffPolicyCurveTests(unittest.TestCase):

    def test_attempt_zero_is_no_sleep(self) -> None:
        # First call doesn't sleep (haven't seen a failure yet).
        bp = _BackoffPolicy(base_sec=2.0, jitter_fraction=0.0)
        self.assertEqual(bp.sleep_for(0), 0.0)

    def test_exponential_growth_without_jitter(self) -> None:
        bp = _BackoffPolicy(
            base_sec=2.0, factor=2.0,
            max_sleep_sec=1000.0, jitter_fraction=0.0,
        )
        self.assertEqual(bp.sleep_for(1), 2.0)
        self.assertEqual(bp.sleep_for(2), 4.0)
        self.assertEqual(bp.sleep_for(3), 8.0)
        self.assertEqual(bp.sleep_for(4), 16.0)

    def test_max_sleep_caps_growth(self) -> None:
        bp = _BackoffPolicy(
            base_sec=2.0, factor=2.0, max_sleep_sec=10.0,
            jitter_fraction=0.0,
        )
        # 2^10 * 2 = 2048 → capped at 10
        self.assertEqual(bp.sleep_for(11), 10.0)

    def test_jitter_lands_in_band(self) -> None:
        # ±25% jitter on a 4s base → 3.0..5.0
        bp = _BackoffPolicy(
            base_sec=4.0, factor=1.0, max_sleep_sec=4.0,
            jitter_fraction=0.25,
        )
        for _ in range(50):
            v = bp.sleep_for(1)
            self.assertGreaterEqual(v, 3.0 - 1e-9)
            self.assertLessEqual(v, 5.0 + 1e-9)


class RateLimitTrackerSemanticsTests(unittest.TestCase):

    def test_clean_run_resets_streak(self) -> None:
        t = _RateLimitTracker(threshold=3)
        t.record(1, "Connection closed by remote host")
        t.record(1, "Connection closed by remote host")
        self.assertEqual(t.consecutive_failures, 2)
        t.record(0, "")
        self.assertEqual(t.consecutive_failures, 0)

    def test_threshold_hit_at_threshold(self) -> None:
        t = _RateLimitTracker(threshold=2)
        t.record(1, "Connection closed by remote host")
        self.assertFalse(t.threshold_hit())
        t.record(1, "Connection closed by remote host")
        self.assertTrue(t.threshold_hit())

    def test_unrelated_failure_resets_streak(self) -> None:
        t = _RateLimitTracker(threshold=3)
        t.record(1, "Connection closed by remote host")
        t.record(1, "Permission denied (publickey)")
        # The "Permission denied" stderr doesn't match a rate-limit
        # pattern → streak resets to 0.
        self.assertEqual(t.consecutive_failures, 0)


class RunWithBackoffRetryTests(unittest.TestCase):
    """Drive a fake invoker through ``_run_with_backoff``."""

    def _make_backend(self, **kwargs):
        # Use SftpBackend with a no-op host; we never call open()
        # so no connection is attempted. _run_with_backoff doesn't
        # touch _require_open / sockets (its caller does), so we
        # can exercise it directly.
        return SftpBackend(
            host="example.invalid",
            port=22, user="x",
            password="placeholder",
            backoff_base_sec=0.001,    # fast for tests
            backoff_max_sec=0.01,
            backoff_max_attempts=4,
            **kwargs,
        )

    def test_retry_succeeds_after_transient_rate_limit(self) -> None:
        backend = self._make_backend()
        events = []
        backend._backoff_callback = (
            lambda kind, payload: events.append((kind, payload))
        )

        attempts = []

        def invoker():
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                # First two calls fail with a rate-limit pattern.
                return subprocess.CompletedProcess(
                    args=[], returncode=1, stdout=b"",
                    stderr=b"Connection closed by remote host\n",
                )
            # Third call succeeds.
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=b"ok", stderr=b"",
            )

        cp = backend._run_with_backoff(
            "sftp", invoker, command_summary="cd /home",
        )
        self.assertEqual(cp.returncode, 0)
        # Two retries + one success = 3 invoker calls.
        self.assertEqual(len(attempts), 3)
        # Two backoff sleeps fired; both surfaced as callback events.
        self.assertEqual(
            sum(1 for k, _ in events if k == "rate_limit_backoff"),
            2,
        )
        # On success the streak counter is back to zero.
        self.assertEqual(backend._rate_limit.consecutive_failures, 0)

    def test_retry_exhaustion_surfaces_rate_limited_error(self) -> None:
        # Threshold of 3 + max_attempts 4 with all calls failing →
        # by end of loop the tracker is past threshold, so the
        # wrapper raises.
        backend = self._make_backend(rate_limit_abort_threshold=3)
        backend._backoff_callback = lambda *a, **kw: None

        def invoker():
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"",
                stderr=b"Connection closed by remote host\n",
            )

        with self.assertRaises(SftpRateLimitedError):
            backend._run_with_backoff(
                "sftp", invoker, command_summary="cd /home",
            )

    def test_non_rate_limit_failure_returns_immediately(self) -> None:
        # A normal (non-throttle) command failure — e.g. file not
        # found — must NOT enter the retry loop. We pin this by
        # asserting only one invoker call happened.
        backend = self._make_backend()
        attempts = []

        def invoker():
            attempts.append(1)
            return subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"",
                stderr=b"No such file or directory\n",
            )

        cp = backend._run_with_backoff(
            "sftp", invoker, command_summary="ls /nope",
        )
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(len(attempts), 1)


if __name__ == "__main__":
    unittest.main()
