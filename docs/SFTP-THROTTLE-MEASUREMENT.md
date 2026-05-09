# SFTP throttle envelope measurement (Hetzner Storage Box)

This file documents the result of running
`scripts/measure_sftp_throttle.py` against the operator's
production destination, and the conclusions for tuning
`SftpBackend`'s backoff defaults.

## Setup

- Backend: `SftpBackend` with SSH ControlMaster multiplexing
  enabled (one persistent master socket per session, reused for
  every `_run_ssh` / `_run_sftp_batch` call).
- Default backoff: `base_sec=2.0, max_sec=300.0, max_attempts=5`
  (current commit-time defaults).
- Measurement-time backoff: deliberately disabled
  (`base_sec=0.001, max_attempts=1`) so timing isn't smoothed by
  retry loops.
- Workload: `N` rapid-fire `list_dir("/")` calls on the
  destination root, no sleep between them.

## Observations

| Burst size | Command outcomes | Median elapsed | Throttle hits |
|------------|------------------|-----------------|---------------|
| 30  | 30 / 30 OK | 3.67 s | 0 |
| 150 | 150 / 150 OK | 3.55 s | 0 |

In neither run did Hetzner trigger any of the pattern strings
the `_RateLimitTracker` watches for (`"Connection closed by
remote host"`, `"connection refused"`, etc.).

The per-command median (~3.6 s) is the SFTP batch overhead per
operation, not network latency — the ControlMaster connection
is already established + reused, so each batch is just spawning
`sftp -b <tmpfile>`, exchanging the ~1 KB ls result, and
exiting.

## Interpretation

**SSH ControlMaster multiplexing is doing the heavy lifting.**
By reusing one master TCP connection for every command, we
never hit Hetzner's per-connection rate limit. That limit is
documented to fire on rapid *new* connections; multiplexed
channels are effectively free.

Real-world failure pattern operators saw during the
`scripts/probe_tree_v4_block.py` run was therefore NOT a
rate-limit hit — it was a **stale-connection drop** triggered
by idle time between commands while local Python decrypted
+ parsed tree blobs. Hetzner closes idle multiplexed channels
after some unspecified interval.

## Implications for backoff defaults

The current defaults (`base=2 s, max=300 s, attempts=5`) are
**already correct** for the failure pattern they handle (an
isolated transient `Connection closed`). No change recommended.

## Follow-up: automatic ControlMaster reconnect (landed)

Building directly on the measurement, **automatic
ControlMaster reconnect** was added in a follow-up PR — when
`_run_with_backoff` sees a stderr matching one of the four
idle-drop signatures (`_RECONNECT_PATTERNS`), it tears down the
existing master via `_teardown_master()`, re-opens it via
`__enter__`, and retries the failing command immediately
without consuming an exponential-backoff sleep slot. Used at
most once per call so a permanently-dead master can't trap us
in an infinite reconnect loop.

The TUI shows `ssh_master_reconnect` events on the backoff
callback so operators see "Reconnecting SFTP master…" in the
log instead of an unexplained pause. Pack-file read cache is
preserved across the reconnect — those temp files are still
valid local copies.

See `arq_validator/sftp.py` (`_RECONNECT_PATTERNS`,
`_teardown_master`, `_reopen_master`) and
`tests/test_sftp_master_reconnect.py` (6 tests).

## Re-running the measurement

```sh
# Burst test (the throttle envelope itself).
python3 scripts/measure_sftp_throttle.py --burst 60 --json

# Recovery test (only fires when the burst trips a throttle).
# Pass --cooldown to cap the wait if the server takes too long.
python3 scripts/measure_sftp_throttle.py --burst 100 --cooldown 600
```

The script reads creds from `.secrets/` / env (same contract as
the integration tests) and aborts cleanly if creds are missing.

## Date of measurement

2026-05-09 — `claude/realdata-integration-and-followups` branch.
Re-run when:

- Hetzner publishes a new throttle policy.
- We add a new SFTP code path that bypasses ControlMaster
  multiplexing (e.g. parallel uploads via separate processes).
- `_run_with_backoff` semantics change.
