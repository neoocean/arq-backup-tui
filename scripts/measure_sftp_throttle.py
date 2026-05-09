#!/usr/bin/env python3
"""Measure the operator's SFTP throttle envelope to tune the
backoff defaults in :class:`arq_validator.sftp.SftpBackend`.

The current defaults are seeded from generic exponential-backoff
intuition (base=2 s, max=300 s, attempts=5). This script runs
a controlled load against the operator's real destination and
reports:

- how many quick-fire commands trip the throttle
- how long the throttle lasts before commands succeed again
- how exponential-backoff vs fixed-interval recovery compares
- whether the chosen ``base`` / ``max_attempts`` are sized right

Output is a JSON block + a human summary you can paste into a
follow-up PR adjusting the defaults. Reads creds from the same
``.secrets/`` setup the integration tests use; aborts cleanly if
they're missing.

Usage::

    python3 scripts/measure_sftp_throttle.py [--burst 60]
                                              [--cooldown 600]
                                              [--json]

This is a diagnostic, not a test. It deliberately *causes*
throttle events, so don't run it during a real backup window.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _open_backend(creds, *, callback=None):
    from arq_validator.sftp import SftpBackend
    backend = SftpBackend(
        creds.host, port=creds.port, user=creds.user,
        password=creds.sftp_password,
        identity_file=creds.identity_file,
        root=creds.root,
        # Pin defaults so the measurement doesn't get smoothed by
        # the wrapper's existing backoff — we want raw timing.
        backoff_base_sec=0.001,
        backoff_max_sec=0.001,
        backoff_max_attempts=1,
        backoff_callback=callback,
    )
    backend.__enter__()
    return backend


def _classify(stderr_text: str) -> str:
    """Match the same patterns the SftpBackend's tracker uses,
    so a 'throttle' here aligns with what the wrapper would
    detect in production."""
    from arq_validator.sftp import _RATE_LIMIT_PATTERNS
    for pat in _RATE_LIMIT_PATTERNS:
        if pat in stderr_text:
            return f"throttle: {pat}"
    if not stderr_text:
        return "ok"
    return "other-error"


def burst(backend, *, count: int) -> List[Dict[str, Any]]:
    """Issue ``count`` rapid-fire ``ls`` commands against the root
    + record per-command timing + outcome."""
    out: List[Dict[str, Any]] = []
    for i in range(count):
        t0 = time.monotonic()
        try:
            entries = backend.list_dir("/")
            elapsed = time.monotonic() - t0
            outcome = "ok"
            err = ""
            count_returned = len(entries)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            err = f"{type(exc).__name__}: {exc}"
            outcome = _classify(err)
            count_returned = -1
        out.append({
            "i": i,
            "elapsed_sec": round(elapsed, 3),
            "outcome": outcome,
            "entries": count_returned,
            "error_excerpt": err[:160] if err else "",
        })
    return out


def measure_recovery(
    backend, *, max_wait_sec: float = 600.0,
) -> Dict[str, Any]:
    """After a throttle, poll ``ls`` every 5 s until it succeeds
    OR ``max_wait_sec`` elapses. Returns the recovery duration."""
    started = time.monotonic()
    polls = 0
    while time.monotonic() - started < max_wait_sec:
        polls += 1
        time.sleep(5.0)
        try:
            backend.list_dir("/")
            return {
                "recovered_after_sec": round(
                    time.monotonic() - started, 1,
                ),
                "polls": polls,
            }
        except Exception:
            continue
    return {
        "recovered_after_sec": None,
        "polls": polls,
        "note": (
            f"did not recover within {max_wait_sec}s; "
            f"server may be in a longer cooldown"
        ),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--burst", type=int, default=60,
        help="how many rapid-fire commands to send",
    )
    p.add_argument(
        "--cooldown", type=float, default=600.0,
        help="max seconds to wait for throttle recovery",
    )
    p.add_argument(
        "--json", action="store_true",
        help="emit the report as JSON",
    )
    args = p.parse_args(argv)

    try:
        from tests.integration._creds import (
            resolve_creds, skip_reason,
        )
    except ImportError as exc:
        sys.exit(f"can't import creds helper: {exc}")
    creds = resolve_creds()
    if creds is None:
        sys.exit(
            f"creds unavailable: {skip_reason() or 'no creds'}"
        )

    backend = _open_backend(creds)
    try:
        attempts = burst(backend, count=args.burst)
        first_throttle = next(
            (a for a in attempts if a["outcome"].startswith("throttle")),
            None,
        )
        report: Dict[str, Any] = {
            "burst_count": len(attempts),
            "throttle_at_index": (
                first_throttle["i"] if first_throttle else None
            ),
            "throttle_pattern": (
                first_throttle["outcome"] if first_throttle else None
            ),
            "ok_count": sum(
                1 for a in attempts if a["outcome"] == "ok"
            ),
            "throttle_count": sum(
                1 for a in attempts
                if a["outcome"].startswith("throttle")
            ),
            "median_ok_elapsed_sec": _median(
                [a["elapsed_sec"] for a in attempts
                 if a["outcome"] == "ok"]
            ),
            "max_ok_elapsed_sec": (
                max(
                    a["elapsed_sec"] for a in attempts
                    if a["outcome"] == "ok"
                ) if any(a["outcome"] == "ok" for a in attempts)
                else None
            ),
            "samples": attempts,
        }
        if first_throttle is not None:
            print(
                f"throttle hit at i={first_throttle['i']} after "
                f"{first_throttle['i']} successful commands; "
                f"measuring recovery (max {args.cooldown}s)…",
                file=sys.stderr,
            )
            report["recovery"] = measure_recovery(
                backend, max_wait_sec=args.cooldown,
            )
        # Recommended defaults derived from observation.
        recovery = report.get("recovery", {})
        recovered_sec = recovery.get("recovered_after_sec")
        if recovered_sec is not None:
            # Aim for backoff to span ~1.2x the observed cooldown
            # over its full attempt envelope.
            target_envelope = recovered_sec * 1.2
            # With factor=2, base=B, attempts=N:
            #   total ≈ B * (2^N - 1)
            # Pick N=5 (matches current default) and solve for B.
            implied_base = max(
                1.0, target_envelope / (2 ** 5 - 1),
            )
            report["recommended_defaults"] = {
                "base_sec": round(implied_base, 1),
                "max_sec": round(
                    max(implied_base * 2 ** 4 * 1.5, 60.0), 1,
                ),
                "max_attempts": 5,
                "rationale": (
                    f"observed recovery in {recovered_sec}s; "
                    f"target backoff envelope {target_envelope:.0f}s "
                    f"with factor=2, attempts=5 → "
                    f"base={implied_base:.1f}s"
                ),
            }
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(_render_summary(report))
        return 0
    finally:
        try:
            backend.__exit__(None, None, None)
        except Exception:
            pass


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    if n % 2:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _render_summary(report: Dict[str, Any]) -> str:
    lines = [
        f"burst commands:    {report['burst_count']}",
        f"  ok:              {report['ok_count']}",
        f"  throttle:        {report['throttle_count']}",
    ]
    if report["throttle_at_index"] is not None:
        lines.append(
            f"first throttle:   command #{report['throttle_at_index']} "
            f"({report['throttle_pattern']})"
        )
    if report["median_ok_elapsed_sec"] is not None:
        lines.append(
            f"per-command:      median {report['median_ok_elapsed_sec']:.2f}s, "
            f"max {report.get('max_ok_elapsed_sec', 0) or 0:.2f}s"
        )
    rec = report.get("recovery")
    if rec is not None:
        sec = rec.get("recovered_after_sec")
        lines.append(
            f"throttle recovery: "
            f"{sec or '> ' + str(rec.get('polls', 0)*5) + 's'} "
            f"(polled {rec.get('polls')} × 5s)"
        )
    rd = report.get("recommended_defaults")
    if rd is not None:
        lines.append("")
        lines.append(
            f"recommended SftpBackend defaults:"
        )
        lines.append(
            f"  backoff_base_sec={rd['base_sec']}, "
            f"backoff_max_sec={rd['max_sec']}, "
            f"backoff_max_attempts={rd['max_attempts']}"
        )
        lines.append(f"  ({rd['rationale']})")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
