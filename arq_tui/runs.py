"""State file format + runtime monitoring for backup / restore /
validate operations.

This is the IPC layer that lets a backup CLI process (started by
cron, by the operator's hand, or spawned by the TUI itself)
publish its progress in a way the TUI can passively observe. The
core idea is dead-simple file-based IPC:

- The CLI writes JSON to ``$XDG_STATE_HOME/arq-backup-tui/runs/<id>.json``
  every time the writer/reader/validator emits a ``ProgressCb``
  event. Writes are atomic (``write tmp + rename``) so a TUI
  reader never sees a half-written file.
- The TUI polls the directory once per second, reads each state
  file, and renders progress + activity. Cancellation is a unix
  signal (``SIGTERM`` / ``SIGINT``) to the writer's PID, which the
  writer's existing ``Backup.cancel()`` machinery already handles
  gracefully.

See ``docs/PLAN-cli-tui-split.md`` §3 for the schema and §2 for
the rationale.

This module exposes three layers:

- :class:`RunStatus`, :class:`RunKind`, :class:`RunRecord` — the
  on-disk shape (frozen dataclasses, ``to_json()`` / ``from_json()``).
- :class:`RunWriter` — the producer side: a context manager that a
  CLI invocation wraps around its work, with an ``update`` method
  that's safe to call from a ``ProgressCb`` (rate-limited writes
  internally).
- :func:`enumerate_runs`, :func:`is_pid_alive`, :func:`mark_stale`,
  :func:`gc_finished_runs` — the consumer side: utilities the TUI
  monitor screen + the ``arq-tui-runs`` CLI build on top of.

Cross-process correctness:

- Atomic write via ``Path.write_bytes`` to a sibling tempfile +
  ``os.replace``. Readers never see a partially-written file.
- mtime is the cheap way to detect "anything changed since last
  poll". The TUI's polling loop compares mtime + does a full read
  only when it advanced.
- No locks. The producer is the sole writer of a given file; the
  TUI never writes back. Multi-host is handled via the ``host``
  field plus host-local ``$XDG_STATE_HOME`` (each host has its
  own runs/ directory).
"""

from __future__ import annotations

import enum
import json
import os
import signal
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional


# Schema version is bumped when an incompatible change to the JSON
# shape lands. Readers older than the bump should refuse to parse
# rather than silently misinterpret fields.
SCHEMA_VERSION = 1

# Cap on how many recent events to retain in ``events_tail``.
# Keeps state files bounded — a long backup writing thousands of
# files would otherwise grow them indefinitely.
EVENTS_TAIL_MAX = 50

# How often a writer flushes during a run. Flushes always happen on
# kind transitions (start, completion, failure); in between we cap
# at one flush per second OR every N events, whichever comes first.
DEFAULT_FLUSH_INTERVAL_SEC = 1.0
DEFAULT_FLUSH_EVENT_COUNT = 100


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunKind(str, enum.Enum):
    BACKUP = "backup"
    RESTORE = "restore"
    VALIDATE = "validate"
    RETENTION = "retention"
    ROTATE_KEYSET = "rotate-keyset"


class RunStatus(str, enum.Enum):
    """Coarse lifecycle state. Producers transition starting →
    running → (completed | failed | cancelled). The reader marks
    a record ``stale`` when its PID has gone away while the
    on-disk status was still ``running``."""

    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"            # walker suspended via Backup.pause();
                                  # transitions back to RUNNING on resume
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"

    @property
    def is_terminal(self) -> bool:
        return self in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.STALE,
        )


# ---------------------------------------------------------------------------
# Record dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RunProgress:
    """Live progress counters. ``files_total`` / ``bytes_total``
    are ``None`` while planning is in progress (e.g. before the
    restore pre-walk emits ``restore_planned``)."""

    files_total: Optional[int] = None
    files_done: int = 0
    bytes_total: Optional[int] = None
    bytes_done: int = 0
    current_path: str = ""
    throughput_bps: float = 0.0
    eta_sec: Optional[float] = None


@dataclass
class RunDestination:
    """Coarse description of where the operation is targeting.

    Captured at start; never updated mid-run."""

    kind: str = "local"   # "local" | "sftp"
    label: str = ""       # operator-friendly nickname (plan name etc.)
    computer_uuid: str = ""


@dataclass
class RunEvent:
    """One serialized progress event. Keeps the original
    ``ProgressCb`` ``(kind, payload)`` shape plus a wall-clock
    timestamp."""

    t: float                 # unix epoch
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    """The on-disk JSON shape, exactly. Field names match the
    schema in ``docs/PLAN-cli-tui-split.md``."""

    schema_version: int = SCHEMA_VERSION
    run_id: str = ""
    kind: str = RunKind.BACKUP.value     # store as str for forward-compat
    status: str = RunStatus.STARTING.value
    started_at: float = 0.0
    finished_at: Optional[float] = None
    pid: int = 0
    host: str = ""
    plan_id: str = ""
    plan_name: str = ""
    destination: RunDestination = field(default_factory=RunDestination)
    progress: RunProgress = field(default_factory=RunProgress)
    result: Optional[Dict[str, Any]] = None
    events_tail: List[RunEvent] = field(default_factory=list)
    error: Optional[str] = None

    # ---- (de)serialization ------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "RunRecord":
        data = json.loads(text)
        sv = data.get("schema_version")
        if sv != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported state-file schema_version: {sv} "
                f"(expected {SCHEMA_VERSION})"
            )
        # Re-hydrate nested dataclasses; tolerate missing optional
        # subdicts so a forward-compat reader doesn't explode on
        # records produced by a bumped writer that's still
        # within the same major schema.
        dest = data.get("destination") or {}
        prog = data.get("progress") or {}
        events = data.get("events_tail") or []
        return cls(
            schema_version=sv,
            run_id=data.get("run_id", ""),
            kind=data.get("kind", RunKind.BACKUP.value),
            status=data.get("status", RunStatus.STARTING.value),
            started_at=float(data.get("started_at") or 0),
            finished_at=(
                float(data["finished_at"])
                if data.get("finished_at") is not None
                else None
            ),
            pid=int(data.get("pid") or 0),
            host=data.get("host", ""),
            plan_id=data.get("plan_id", ""),
            plan_name=data.get("plan_name", ""),
            destination=RunDestination(
                kind=dest.get("kind", "local"),
                label=dest.get("label", ""),
                computer_uuid=dest.get("computer_uuid", ""),
            ),
            progress=RunProgress(
                files_total=prog.get("files_total"),
                files_done=int(prog.get("files_done") or 0),
                bytes_total=prog.get("bytes_total"),
                bytes_done=int(prog.get("bytes_done") or 0),
                current_path=prog.get("current_path") or "",
                throughput_bps=float(prog.get("throughput_bps") or 0.0),
                eta_sec=prog.get("eta_sec"),
            ),
            result=data.get("result"),
            events_tail=[
                RunEvent(
                    t=float(ev.get("t") or 0),
                    kind=ev.get("kind", ""),
                    payload=dict(ev.get("payload") or {}),
                )
                for ev in events
            ],
            error=data.get("error"),
        )


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------


def default_state_dir() -> Path:
    """Return the default state-file directory.

    Honors ``XDG_STATE_HOME`` and falls back to
    ``~/.local/state/arq-backup-tui/runs`` on systems (macOS /
    Linux non-XDG) where it's not set. The directory is created on
    first use; callers don't need to ``mkdir`` themselves."""
    base = os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state",
    )
    return Path(base) / "arq-backup-tui" / "runs"


def state_file_path(run_id: str, *, state_dir: Optional[Path] = None) -> Path:
    """Return the absolute path of the state file for ``run_id``."""
    sd = Path(state_dir) if state_dir is not None else default_state_dir()
    return sd / f"{run_id}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically — same-directory
    tempfile + ``os.replace``. Same-directory is required so the
    rename is on a single filesystem (``os.replace`` is then
    guaranteed atomic on POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(
        f".tmp.{os.getpid()}.{int(time.time() * 1000) % 100000}",
    )
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except OSError:
        # Best-effort cleanup of the temp before re-raising.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def new_run_id() -> str:
    """UUID4 stringified — collision-free across hosts. The TUI
    looks up runs by ID so they need only to be unique on this
    host (writers are sequential per-host)."""
    return str(uuid.uuid4()).upper()


# ---------------------------------------------------------------------------
# Producer side — RunWriter
# ---------------------------------------------------------------------------


class RunWriter:
    """Producer-side state file manager.

    Wrap a CLI's main() in a ``with RunWriter(record) as rw:``
    and call ``rw.event(kind, **payload)`` from your ``ProgressCb``
    closure. Termination state (status=completed / failed /
    cancelled) is set automatically based on whether the with-block
    exits normally or via an exception.
    """

    def __init__(
        self,
        record: RunRecord,
        *,
        state_dir: Optional[Path] = None,
        flush_interval_sec: float = DEFAULT_FLUSH_INTERVAL_SEC,
        flush_event_count: int = DEFAULT_FLUSH_EVENT_COUNT,
    ) -> None:
        self.record = record
        self.path = state_file_path(record.run_id, state_dir=state_dir)
        self.flush_interval_sec = flush_interval_sec
        self.flush_event_count = flush_event_count
        # Ring buffer for events_tail; the dataclass field stays in
        # sync via _flush.
        self._events: Deque[RunEvent] = deque(maxlen=EVENTS_TAIL_MAX)
        self._unflushed_event_count = 0
        self._last_flush_t = 0.0
        # Throughput tracker — bytes & wall-clock pairs, last 30 s.
        self._tput_samples: Deque[tuple] = deque()
        self._tput_window_sec = 30.0

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "RunWriter":
        self.record.pid = os.getpid()
        if not self.record.started_at:
            self.record.started_at = time.time()
        if not self.record.host:
            try:
                import socket
                self.record.host = socket.gethostname()
            except Exception:
                self.record.host = "unknown"
        self.record.status = RunStatus.RUNNING.value
        self._force_flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.record.finished_at = time.time()
        if exc_type is None:
            # Caller may have already set status (e.g. CANCELLED on
            # signal); only auto-promote to COMPLETED if still
            # running.
            if self.record.status == RunStatus.RUNNING.value:
                self.record.status = RunStatus.COMPLETED.value
        elif issubclass(exc_type, KeyboardInterrupt):
            self.record.status = RunStatus.CANCELLED.value
        else:
            self.record.status = RunStatus.FAILED.value
            self.record.error = f"{exc_type.__name__}: {exc}"
        self._force_flush()
        # Fire the operator-facing notification (notify-send /
        # osascript / shell hook) per the loaded NotificationConfig.
        # Best-effort: any exception here is swallowed because we
        # never want a notification daemon hiccup to corrupt the
        # caller's exit path. Tests can suppress the hook entirely
        # via ``ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS=1`` so the
        # full unit-test run stays silent.
        if not os.environ.get("ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS"):
            try:
                from .notifications import notify_run_finished
                notify_run_finished(self.record)
            except Exception:
                pass
        # Don't suppress exceptions — caller still needs to see them.
        return None

    # -- updates ---------------------------------------------------------

    def event(self, kind: str, **payload: Any) -> None:
        """Record one ProgressCb event. Updates the running counters
        for known kinds; arbitrary kinds land in events_tail only."""
        ev = RunEvent(t=time.time(), kind=kind, payload=dict(payload))
        self._events.append(ev)
        self.record.events_tail = list(self._events)
        self._apply_to_progress(ev)
        self._unflushed_event_count += 1
        self._flush_if_due()

    def _apply_to_progress(self, ev: RunEvent) -> None:
        """Update :class:`RunProgress` counters based on the event
        kind. Recognized kinds mirror what the writer / reader /
        validator actually emit (see ``arq_writer/backup.py``,
        ``arq_reader/restore.py``)."""
        p = self.record.progress
        kind = ev.kind
        payload = ev.payload
        if kind == "restore_planned":
            tf = payload.get("total_files")
            tb = payload.get("total_bytes")
            if tf is not None:
                p.files_total = int(tf)
            if tb is not None:
                p.bytes_total = int(tb)
        elif kind in ("file_written", "file_restored",
                      "audit_file_verified"):
            p.files_done += 1
            sz = int(payload.get("size") or 0)
            p.bytes_done += sz
            path = payload.get("path") or payload.get("rel_path") or ""
            if path:
                p.current_path = str(path)
            self._update_throughput()
        elif kind == "file_reused":
            p.files_done += 1
            path = payload.get("path") or ""
            if path:
                p.current_path = str(path)
        elif kind == "tree_written" or kind == "tree_restored":
            # Trees count toward "files done" the way the existing
            # ProgressPanel models them — covered units, not
            # plain-bytes throughput.
            pass

    def _update_throughput(self) -> None:
        now = time.time()
        bd = self.record.progress.bytes_done
        self._tput_samples.append((now, bd))
        cutoff = now - self._tput_window_sec
        while self._tput_samples and self._tput_samples[0][0] < cutoff:
            self._tput_samples.popleft()
        if len(self._tput_samples) >= 2:
            t0, b0 = self._tput_samples[0]
            t1, b1 = self._tput_samples[-1]
            dt = max(t1 - t0, 0.001)
            rate = max(b1 - b0, 0) / dt
            self.record.progress.throughput_bps = rate
            if (
                self.record.progress.bytes_total
                and rate > 0
            ):
                remaining = max(
                    self.record.progress.bytes_total - bd, 0,
                )
                self.record.progress.eta_sec = remaining / rate

    def set_destination(
        self, *, kind: str = "local", label: str = "",
        computer_uuid: str = "",
    ) -> None:
        self.record.destination = RunDestination(
            kind=kind, label=label, computer_uuid=computer_uuid,
        )

    def set_result(self, result: Dict[str, Any]) -> None:
        self.record.result = dict(result)

    def cancel(self) -> None:
        """Mark the run as cancelled. Caller is responsible for
        actually stopping the work (signal handler, etc.)."""
        self.record.status = RunStatus.CANCELLED.value
        self._force_flush()

    # -- flushing --------------------------------------------------------

    def _flush_if_due(self) -> None:
        now = time.time()
        if (
            self._unflushed_event_count >= self.flush_event_count
            or (now - self._last_flush_t) >= self.flush_interval_sec
        ):
            self._force_flush()

    def _force_flush(self) -> None:
        _atomic_write(self.path, self.record.to_json().encode("utf-8"))
        self._unflushed_event_count = 0
        self._last_flush_t = time.time()


# ---------------------------------------------------------------------------
# Consumer side — TUI / CLI utilities
# ---------------------------------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    """Return True iff a process with PID ``pid`` is currently
    alive on this host. ``os.kill(pid, 0)`` raises :class:`OSError`
    when no such process exists, succeeds otherwise."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — treat as alive
        # since the goal is just liveness.
        return True
    except OSError:
        return False


def enumerate_runs(
    *, state_dir: Optional[Path] = None,
) -> List[RunRecord]:
    """Read every state file in the runs directory and return the
    parsed records, sorted started_at ascending. Files that fail
    to parse are skipped (with their path silently discarded — a
    half-written record from a crash shouldn't break the listing)."""
    sd = Path(state_dir) if state_dir is not None else default_state_dir()
    if not sd.is_dir():
        return []
    out: List[RunRecord] = []
    for entry in sorted(sd.iterdir()):
        if entry.suffix != ".json" or not entry.is_file():
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            rec = RunRecord.from_json(text)
        except (ValueError, KeyError):
            continue
        out.append(rec)
    out.sort(key=lambda r: r.started_at)
    return out


def mark_stale(
    record: RunRecord, *,
    state_dir: Optional[Path] = None,
) -> None:
    """If a record's on-disk status is ``running`` but its PID is
    gone, rewrite it as ``stale``. Idempotent: re-running on an
    already-stale record is a no-op."""
    if record.status != RunStatus.RUNNING.value:
        return
    if is_pid_alive(record.pid):
        return
    record.status = RunStatus.STALE.value
    record.finished_at = time.time()
    path = state_file_path(record.run_id, state_dir=state_dir)
    _atomic_write(path, record.to_json().encode("utf-8"))


def gc_finished_runs(
    *, state_dir: Optional[Path] = None,
    older_than_sec: float = 30 * 24 * 3600,
) -> int:
    """Remove state files for runs that finished more than
    ``older_than_sec`` ago. Returns the count removed.

    Live / running runs are never touched. Stale records (writer
    crashed) are GC'd by the same age threshold."""
    sd = Path(state_dir) if state_dir is not None else default_state_dir()
    if not sd.is_dir():
        return 0
    now = time.time()
    removed = 0
    for rec in enumerate_runs(state_dir=sd):
        if not RunStatus(rec.status).is_terminal:
            continue
        ref_t = rec.finished_at or rec.started_at
        if now - ref_t < older_than_sec:
            continue
        path = state_file_path(rec.run_id, state_dir=sd)
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def signal_cancel(record: RunRecord) -> bool:
    """Send SIGTERM to the writer's PID so it can graceful-cancel.
    Returns True iff the signal was delivered (PID alive +
    permission OK). The writer's exit handler is responsible for
    flipping the status to ``cancelled``; this function only
    initiates the cancellation."""
    if not is_pid_alive(record.pid):
        return False
    try:
        os.kill(record.pid, signal.SIGTERM)
        return True
    except OSError:
        return False


@contextmanager
def run_writer_context(
    *, kind: RunKind,
    plan_id: str = "",
    plan_name: str = "",
    state_file: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    run_id: Optional[str] = None,
):
    """Convenience: build a :class:`RunRecord` + :class:`RunWriter`
    in one call. ``state_file`` overrides ``state_dir`` + ``run_id``
    when set (matches the CLI's ``--state-file`` flag).

    Usage from a CLI::

        with run_writer_context(
            kind=RunKind.BACKUP, plan_id=plan.plan_id,
            plan_name=plan.name, state_file=args.state_file,
        ) as rw:
            def cb(kind, payload):
                rw.event(kind, **payload)
            build_backup(..., callback=cb)
    """
    rid = run_id or new_run_id()
    if state_file is not None:
        sf = Path(state_file)
        if sf.suffix != ".json":
            raise ValueError(
                f"state_file must end in .json: {state_file}",
            )
        # Override run_id from the file stem so
        # ``arq-tui-runs show <id>`` works against it.
        rid = sf.stem
        sd = sf.parent
    else:
        sd = state_dir
    record = RunRecord(
        run_id=rid,
        kind=kind.value,
        plan_id=plan_id,
        plan_name=plan_name,
    )
    writer = RunWriter(record, state_dir=sd)
    with writer as rw:
        yield rw
