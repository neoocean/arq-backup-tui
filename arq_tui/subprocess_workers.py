"""Subprocess-spawning workers that drive backups via the
``arq-backup`` CLI and watch the state file for progress events.

This is the "dual-mode" half of :mod:`arq_tui.workers`. The
in-process workers there spawn a sibling Python thread and call
``arq_writer.Backup`` directly; the subprocess workers here fork
``python3 -m arq_writer create …`` (or an equivalent reader CLI)
with ``--state-file`` and poll the resulting JSON file for live
progress.

Why two modes:

- **Subprocess** is the production path. The same CLI invocation
  cron and systemd would launch is also what the TUI runs, so
  monitoring + cancel work uniformly across "TUI launched it" /
  "cron launched it" / "operator launched it manually". A
  ``SIGTERM`` to the writer's PID flips status=cancelled via the
  writer's exit handler. Failures land in
  ``RunRecord.error`` for retrospective analysis.

- **In-process** stays around as the legacy worker (``workers.py``)
  for tests + corner cases the CLI doesn't cover yet (multi-source
  plans, SFTP destinations — both of which the spawn path
  short-circuits back to legacy mode for). Tests opt into legacy
  mode explicitly via ``ARQ_TUI_IN_PROCESS=1`` so a CI run isn't
  forced through subprocess + state-file polling for unit-test
  speed.

The screen-level glue (:class:`arq_tui.screens.backup_run.BackupRunScreen`)
picks one or the other based on whether the plan + destination
fit the subprocess constraints — see :func:`subprocess_eligible`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from textual.message import Message  # for type hints on _post()

from .runs import (
    RunKind,
    RunRecord,
    RunStatus,
    new_run_id,
    state_file_path,
)
# Reuse the in-process worker's message classes so the screen
# handlers (on_worker_event / on_worker_finished / on_worker_failed)
# don't have to special-case which mode produced them. Textual
# routes messages by class identity, so identical-shape but
# distinct classes would not be picked up by a single handler.
from .workers import (
    WorkerEvent,
    WorkerFailed,
    WorkerFinished,
)


# Polling cadence for state files. Each tick decodes the file +
# diffs the events_tail against what we've already forwarded to
# the screen. 1 Hz matches the writer's flush cadence and keeps
# UI updates smooth without spinning the CPU.
_POLL_INTERVAL_SEC = 1.0


def subprocess_eligible(plan, dest_kind: str) -> bool:
    """Return True iff the subprocess + state-file path can drive
    this plan without losing functionality.

    Both local and SFTP destinations are now supported (the CLI
    grew ``--sftp-host``/``--sftp-user``/``--sftp-path``/
    ``--sftp-identity-file``/``--sftp-password-env`` args). Both
    single- and multi-source plans are supported (the CLI takes
    ``nargs='+'`` for the positional source list).

    The only remaining gate is destination-kind sanity: anything
    we don't recognize falls back to in-process so we don't pass
    a bogus dest spec to the subprocess.
    """
    if dest_kind not in ("local", "sftp"):
        return False
    if not plan.sources:
        return False
    return True


# ---------------------------------------------------------------------------
# SubprocessBackupWorker
# ---------------------------------------------------------------------------


class SubprocessBackupWorker:
    """Drive a backup by spawning the ``arq-backup create`` CLI.

    Constructor builds the argv + env, ``start()`` launches the
    subprocess + the polling thread, ``cancel()`` sends SIGTERM
    so the writer's ``RunWriter`` context catches it and flips
    the on-disk status to ``cancelled``.

    Posts the same ``WorkerEvent`` / ``WorkerFinished`` /
    ``WorkerFailed`` messages the in-process worker does, so the
    screen's handlers can stay agnostic of which backend produced
    the events.
    """

    def __init__(
        self, target, *,
        plan,
        password: str,
        state_dir: Optional[Path] = None,
        run_id: Optional[str] = None,
        executable: Optional[str] = None,
    ) -> None:
        self.target = target
        self.app = target.app
        self.plan = plan
        self.password = password
        self.state_dir = state_dir
        self.run_id = run_id or new_run_id()
        self.state_file = state_file_path(
            self.run_id, state_dir=state_dir,
        )
        self.executable = executable or sys.executable
        self._proc: Optional[subprocess.Popen] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Track which event indices we've already forwarded so
        # repeated polls don't re-emit them.
        self._last_event_index = 0

    # -- public API ----------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("worker already started")
        argv = self._build_argv()
        env = dict(os.environ)
        # CLI reads the password from --password-env so it never
        # appears in argv (which would leak through ps output on
        # multi-user hosts). The env var is scoped to this child
        # process.
        env["ARQ_BACKUP_PW_TUI"] = self.password
        # Make sure the state file's parent exists before spawn.
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            argv, env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True,
        )
        self._poll_thread.start()

    def cancel(self) -> None:
        """Signal SIGTERM so the writer's RunWriter context handles
        it as KeyboardInterrupt → status=cancelled."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGTERM)
            except OSError:
                pass

    def pause(self) -> None:
        """Forward pause request to the writer subprocess via
        SIGUSR1. The writer CLI's signal handler (see
        arq_writer/cli.py:_install_pause_signal_handlers)
        translates it to ``Backup.pause()``.

        Best-effort: SIGUSR1 is POSIX-only; on Windows the
        send silently fails. Operators on Windows fall back
        to ``ARQ_TUI_IN_PROCESS=1`` for pause/resume.
        """
        if not hasattr(signal, "SIGUSR1"):
            return
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGUSR1)
            except OSError:
                pass

    def resume(self) -> None:
        """Forward resume request via SIGUSR2; mirror of
        :meth:`pause`."""
        if not hasattr(signal, "SIGUSR2"):
            return
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGUSR2)
            except OSError:
                pass

    def join(self, timeout: Optional[float] = None) -> None:
        if self._poll_thread is not None:
            self._poll_thread.join(timeout)

    # -- internals -----------------------------------------------------

    def _build_argv(self) -> list:
        """Translate :class:`Plan` fields into ``arq-backup create``
        argv. Caller is responsible for ensuring
        :func:`subprocess_eligible` returned True before invoking.

        Source list goes in as positional args (the CLI accepts
        ``nargs='+'``); destination is either ``--dest <path>`` for
        local plans or the ``--sftp-host``/``--sftp-port``/
        ``--sftp-user``/``--sftp-path``/``--sftp-identity-file``/
        ``--sftp-password-env`` family for SFTP plans.
        """
        plan = self.plan
        argv = [
            self.executable, "-m", "arq_writer",
            "create",
        ]
        # Sources first — CLI keeps positional args before flags.
        for src in plan.sources:
            argv.append(src)
        # Destination wiring depends on kind.
        if plan.destination_kind == "sftp":
            d = plan.destination or {}
            argv += [
                "--sftp-host", str(d.get("host") or ""),
                "--sftp-port", str(int(d.get("port") or 22)),
                "--sftp-user", str(d.get("user") or ""),
                "--sftp-path", str(d.get("path") or ""),
            ]
            if d.get("identity_file"):
                argv += [
                    "--sftp-identity-file",
                    str(d.get("identity_file")),
                ]
            # SSH password (when password auth is used) is read from
            # an env var so it never appears in argv. The screen
            # primes ARQ_SFTP_PW_TUI in env when the cached SFTP
            # auth contains a password.
            if d.get("password_env"):
                argv += [
                    "--sftp-password-env",
                    str(d.get("password_env")),
                ]
        else:
            argv += ["--dest", str(plan.destination.get("path") or "")]
        argv += [
            "--password-env", "ARQ_BACKUP_PW_TUI",
            "--state-file", str(self.state_file),
            "--backup-name", plan.name or "TUI backup",
            "--chunker", plan.chunker or "default",
        ]
        if plan.use_packs:
            argv.append("--use-packs")
        if plan.dedup_against_existing:
            argv.append("--dedup-against-existing")
        if plan.max_file_bytes:
            argv.extend(["--max-file-bytes", str(plan.max_file_bytes)])
        for pat in plan.exclude_globs:
            argv.extend(["--exclude-glob", pat])
        for pat in plan.exclude_regexes:
            argv.extend(["--exclude-regex", pat])
        if plan.use_apfs_snapshot:
            argv.append("--use-apfs-snapshot")
        return argv

    def _poll_loop(self) -> None:
        """Watch the state file until the subprocess exits.

        Each tick, decode the file and forward any new events to
        the screen as ``WorkerEvent``. When the subprocess process
        exits, read the final state and post ``WorkerFinished`` /
        ``WorkerFailed`` based on the recorded ``status``.
        """
        proc = self._proc
        assert proc is not None
        last_seen_status: Optional[str] = None

        while not self._stop.is_set():
            self._drain_state_file_once()
            rc = proc.poll()
            if rc is not None:
                # Subprocess exited. Drain one last time, then
                # finalize.
                self._drain_state_file_once()
                self._finalize(rc)
                return
            time.sleep(_POLL_INTERVAL_SEC)
        # If externally stopped, still try to finalize.
        if proc.poll() is not None:
            self._finalize(proc.returncode)

    def _drain_state_file_once(self) -> None:
        if not self.state_file.is_file():
            return
        try:
            text = self.state_file.read_text(encoding="utf-8")
        except OSError:
            return
        try:
            record = RunRecord.from_json(text)
        except (ValueError, KeyError):
            return
        # Forward any newly-arrived events.
        events = record.events_tail or []
        if len(events) > self._last_event_index:
            for ev in events[self._last_event_index:]:
                self._post(WorkerEvent(ev.kind, dict(ev.payload)))
            self._last_event_index = len(events)

    def _finalize(self, returncode: int) -> None:
        record: Optional[RunRecord] = None
        if self.state_file.is_file():
            try:
                record = RunRecord.from_json(
                    self.state_file.read_text(encoding="utf-8"),
                )
            except (ValueError, KeyError, OSError):
                record = None
        # Drain + close the subprocess pipes either way so we don't
        # leak file descriptors past the worker's lifetime. We
        # capture stderr first because some failure paths surface
        # it, then close both ends.
        stderr = b""
        proc = self._proc
        if proc is not None:
            for stream in (proc.stderr, proc.stdout):
                if stream is None:
                    continue
                try:
                    blob = stream.read() or b""
                    if stream is proc.stderr:
                        stderr = blob
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
        if record is None:
            self._post(WorkerFailed(
                f"backup CLI exited rc={returncode} without "
                f"writing a state file",
                stderr.decode("utf-8", "replace"),
            ))
            return
        if record.status == RunStatus.COMPLETED.value:
            self._post(WorkerFinished(record.result or {}))
        elif record.status == RunStatus.CANCELLED.value:
            self._post(WorkerFailed(
                "BackupCancelled: cancelled by user",
            ))
        else:
            self._post(WorkerFailed(
                record.error or f"backup CLI status={record.status}",
            ))

    def _post(self, msg: Message) -> None:
        try:
            self.app.call_from_thread(
                self.target.post_message, msg,
            )
        except Exception:
            # App may have shut down between events — drop
            # silently rather than crash the polling thread.
            pass
