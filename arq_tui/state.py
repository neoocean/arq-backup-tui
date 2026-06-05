"""Persistent state for the TUI.

Two on-disk stores live here:

- :class:`PlanRegistry` — one JSON file per plan in ``plans/``.
  M3 adds save / delete; M1+ can read whatever's there.
- :class:`DestinationStore` — the list of recently-opened
  destinations (local paths, SFTP coordinates) shown by the
  backup-set browser. Lives in ``recent_destinations.json``.

Plus a session-scoped credential cache (:class:`CredentialCache`)
that holds passwords + SFTP secrets in memory only — it never
touches the disk.

Storage layout (created on first save):

    $XDG_CONFIG_HOME/arq-backup-tui/
    ├── config.toml
    ├── plans/
    │   └── <plan-uuid>.json
    └── recent_destinations.json
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


def _default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(
        Path.home() / ".config"
    )
    return Path(base) / "arq-backup-tui"


@dataclass
class Plan:
    """A backup plan record.

    The ``chunker`` field selects the writer's default chunker.
    ``per_source_chunkers`` (optional) overrides ``chunker`` for
    specific sources by absolute source path → chunker name. Used
    by the plan wizard's "different chunker for this source"
    affordance and matches Arq.app's per-folder ``useBuzhash``
    toggle.

    ``exclude_globs`` / ``exclude_regexes`` / ``exclude_gitignore_lines``
    feed :class:`arq_writer.ExclusionRules` at run time. Empty lists
    mean "include everything"; the writer's default is also empty.

    ``max_file_bytes`` skips any file larger than the threshold (the
    writer emits a ``file_skipped_size`` event for each); ``None``
    disables the limit. ``use_apfs_snapshot`` opts into walking an
    APFS snapshot of each source on macOS; on other platforms the
    writer emits ``apfs_snapshot_skipped`` and falls back to the
    live walk.

    ``retention`` is a free-form dict round-tripped to disk, with
    the same field names as :class:`arq_writer.RetentionPolicy`
    (e.g. ``{"keep_last_n": 10, "keep_daily": 7}``). Empty dict =
    keep everything (no pruning).
    """

    plan_id: str = ""
    name: str = ""
    # Where this plan came from. ``"tui"`` (default) = created /
    # owned by this tool, fully editable. ``"arq"`` = mirrored
    # read-only from a locally-installed Arq.app's server.db (see
    # :mod:`arq_tui.arq_app`); the wizard refuses to overwrite it
    # and it carries no on-disk plan file in our config dir.
    origin: str = "tui"
    sources: List[str] = field(default_factory=list)
    destination_kind: str = "local"   # "local" | "sftp"
    destination: dict = field(default_factory=dict)
    # Optional additional destinations the plan also targets. Each
    # dict carries its own ``kind`` ("local"/"sftp") + the kind-
    # appropriate connection fields (path / host / port / user /
    # path / identity_file). The primary ``destination`` /
    # ``destination_kind`` above counts as the first destination —
    # ``iter_destinations()`` yields it ahead of these extras so
    # operators of single-destination plans see no change.
    additional_destinations: List[dict] = field(default_factory=list)
    chunker: str = "default"
    per_source_chunkers: dict = field(default_factory=dict)
    use_packs: bool = True
    dedup_against_existing: bool = True
    exclude_globs: List[str] = field(default_factory=list)
    exclude_regexes: List[str] = field(default_factory=list)
    exclude_gitignore_lines: List[str] = field(default_factory=list)
    max_file_bytes: Optional[int] = None
    use_apfs_snapshot: bool = False
    # Time Machine exclusion override (E2-new). When False
    # (default = Arq.app v8 convention) the walker skips files
    # carrying ``com.apple.metadata:com_apple_backup_excludeItem``
    # xattr. When True the operator wants those files backed up
    # despite the OS hint. Threads through to
    # ``Backup(skip_tm_excludes=…)`` at backup time.
    skip_tm_excludes: bool = False
    retention: dict = field(default_factory=dict)
    last_run_iso: str = ""
    # Schedule for periodic execution. Empty dict = unscheduled.
    # Recognised keys (per arq_tui.scheduling.ScheduleSpec):
    #   {"cron_expr": "0 3 * * *"}    — cron-style daily
    #   {"interval_sec": 3600}        — every N seconds
    # Setting both keys is rejected by ScheduleSpec.__post_init__.
    schedule: dict = field(default_factory=dict)

    def iter_destinations(self) -> List[dict]:
        """Yield every destination this plan targets, primary first.

        Returns a list of ``{"kind": …, …}`` dicts. The primary
        destination is built from ``destination_kind`` +
        ``destination``; subsequent entries come from
        ``additional_destinations`` verbatim. Empty primary
        (no path / no host) is skipped so a plan that only uses
        the additional list isn't double-counted.
        """
        out: List[dict] = []
        primary = dict(self.destination or {})
        primary["kind"] = self.destination_kind
        # Skip the primary if it carries no usable connection
        # info — operators sometimes leave the legacy primary
        # blank when migrating to the additional list.
        if primary.get("path") or primary.get("host"):
            out.append(primary)
        for d in self.additional_destinations:
            normalised = dict(d)
            # Default kind to local if the operator forgot it.
            normalised.setdefault("kind", "local")
            out.append(normalised)
        return out


class PlanRegistry:
    """File-system-backed plan registry.

    For M1 the registry is read-only and returns an empty list when
    the on-disk directory doesn't exist (the common case on first
    launch). M3 adds save / delete.
    """

    def __init__(self, *, config_dir: Optional[Path] = None) -> None:
        self.config_dir = (
            Path(config_dir) if config_dir is not None
            else _default_config_dir()
        )
        self.plans_dir = self.config_dir / "plans"

    def list_plans(self) -> List[Plan]:
        """Return every plan currently on disk, sorted by name.

        Missing directory → empty list (the user has no plans yet).
        Malformed plan files are skipped silently — UI continues to
        function without surfacing a misformed-file error every
        launch.
        """
        if not self.plans_dir.is_dir():
            return []
        out: List[Plan] = []
        # JSON parsing happens lazily inside try/except so a single
        # bad file can't break the whole list.
        for p in sorted(self.plans_dir.iterdir()):
            if not p.is_file() or p.suffix != ".json":
                continue
            try:
                with p.open("rb") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            mfb_raw = data.get("max_file_bytes")
            try:
                max_file_bytes = (
                    int(mfb_raw) if mfb_raw not in (None, "") else None
                )
            except (TypeError, ValueError):
                max_file_bytes = None
            out.append(Plan(
                plan_id=str(data.get("plan_id") or ""),
                name=str(data.get("name") or ""),
                origin=str(data.get("origin") or "tui"),
                sources=[
                    str(s) for s in data.get("sources") or []
                ],
                destination_kind=str(
                    data.get("destination_kind") or "local"
                ),
                destination=dict(data.get("destination") or {}),
                additional_destinations=[
                    dict(d) for d in
                    (data.get("additional_destinations") or [])
                ],
                chunker=str(data.get("chunker") or "default"),
                per_source_chunkers=dict(
                    data.get("per_source_chunkers") or {}
                ),
                use_packs=bool(data.get("use_packs", True)),
                dedup_against_existing=bool(
                    data.get("dedup_against_existing", True)
                ),
                exclude_globs=[
                    str(s) for s in data.get("exclude_globs") or []
                ],
                exclude_regexes=[
                    str(s) for s in data.get("exclude_regexes") or []
                ],
                exclude_gitignore_lines=[
                    str(s)
                    for s in data.get("exclude_gitignore_lines") or []
                ],
                max_file_bytes=max_file_bytes,
                use_apfs_snapshot=bool(
                    data.get("use_apfs_snapshot", False)
                ),
                skip_tm_excludes=bool(
                    data.get("skip_tm_excludes", False)
                ),
                retention=dict(data.get("retention") or {}),
                last_run_iso=str(data.get("last_run_iso") or ""),
                schedule=dict(data.get("schedule") or {}),
            ))
        out.sort(key=lambda pl: pl.name.lower())
        return out

    def save(self, plan: Plan) -> Path:
        """Persist ``plan`` to ``plans/<plan_id>.json``.

        Caller is responsible for assigning a stable ``plan_id``
        (typically a UUID generated at wizard-submit time).
        Returns the absolute path that was written.
        """
        if not plan.plan_id:
            raise ValueError("plan.plan_id is required")
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        path = self.plans_dir / f"{plan.plan_id}.json"
        data = {
            "plan_id": plan.plan_id,
            "name": plan.name,
            "origin": plan.origin,
            "sources": list(plan.sources),
            "destination_kind": plan.destination_kind,
            "destination": dict(plan.destination),
            "additional_destinations": [
                dict(d) for d in plan.additional_destinations
            ],
            "chunker": plan.chunker,
            "per_source_chunkers": dict(plan.per_source_chunkers),
            "use_packs": plan.use_packs,
            "dedup_against_existing": plan.dedup_against_existing,
            "exclude_globs": list(plan.exclude_globs),
            "exclude_regexes": list(plan.exclude_regexes),
            "exclude_gitignore_lines": list(plan.exclude_gitignore_lines),
            "max_file_bytes": plan.max_file_bytes,
            "use_apfs_snapshot": plan.use_apfs_snapshot,
            "skip_tm_excludes": plan.skip_tm_excludes,
            "retention": dict(plan.retention),
            "last_run_iso": plan.last_run_iso,
            "schedule": dict(plan.schedule),
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return path

    def delete(self, plan_id: str) -> bool:
        """Remove ``plans/<plan_id>.json``. Returns ``True`` if a
        file was removed, ``False`` otherwise."""
        path = self.plans_dir / f"{plan_id}.json"
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def mark_run(
        self, plan_id: str, *, when_iso: Optional[str] = None,
    ) -> bool:
        """Stamp a plan's ``last_run_iso`` to ``when_iso`` (or now,
        UTC ISO-8601). Returns True if the plan existed + was
        updated, False if not found.

        Called by the BackupRunScreen when a worker reports
        finished/failed so HomeScreen's plan list shows a fresh
        timestamp instead of "never run" forever. Failures are
        stamped too — operators want "last attempt 2 hours ago,
        failed" surfaced just as much as a clean success."""
        if when_iso is None:
            from datetime import datetime, timezone
            when_iso = datetime.now(timezone.utc).isoformat(
                timespec="seconds",
            )
        path = self.plans_dir / f"{plan_id}.json"
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        data["last_run_iso"] = when_iso
        # Atomic write — same write-tmp + os.replace pattern used
        # everywhere else so a crash mid-update can't corrupt the
        # plan file.
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            return False
        return True


# ---------------------------------------------------------------------------
# Recent destinations
# ---------------------------------------------------------------------------


@dataclass
class Destination:
    """A backup destination as remembered by the destination store.

    For local destinations only ``path`` is meaningful; for SFTP
    every field except ``path`` is consulted (path is informational
    in that case). Passwords / identity files are NOT persisted —
    the ``CredentialCache`` holds those in session memory only.
    """

    kind: str = "local"   # "local" | "sftp"
    label: str = ""        # user-friendly nickname
    path: str = ""         # local fs path (kind=local) or remote root (sftp)
    host: str = ""
    port: int = 22
    user: str = ""
    identity_file: str = ""
    # ``"tui"`` (default) = manually opened / remembered by this
    # tool. ``"arq"`` = mirrored from a locally-installed Arq.app's
    # storage_locations (read-only origin marker; see
    # :mod:`arq_tui.arq_app`).
    origin: str = "tui"

    def display(self) -> str:
        """One-line summary for list views."""
        if self.label:
            return self.label
        if self.kind == "sftp":
            return f"sftp://{self.user or '?'}@{self.host}:{self.port}{self.path}"
        return self.path or "(local)"


class DestinationStore:
    """File-system-backed list of recently-opened destinations.

    The order is most-recently-touched first; each ``add_or_touch``
    call moves an existing entry to the front (matched by
    ``(kind, host, port, user, path)`` tuple — i.e. the
    fully-qualifying coordinates).
    """

    FILENAME = "recent_destinations.json"
    MAX_ENTRIES = 32

    def __init__(self, *, config_dir: Optional[Path] = None) -> None:
        self.config_dir = (
            Path(config_dir) if config_dir is not None
            else _default_config_dir()
        )
        self.path = self.config_dir / self.FILENAME

    def _key(self, d: Destination):
        return (d.kind, d.host, d.port, d.user, d.path)

    def list(self) -> List[Destination]:
        if not self.path.is_file():
            return []
        try:
            with self.path.open("rb") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        out: List[Destination] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            out.append(Destination(
                kind=str(item.get("kind") or "local"),
                label=str(item.get("label") or ""),
                path=str(item.get("path") or ""),
                host=str(item.get("host") or ""),
                port=int(item.get("port") or 22),
                user=str(item.get("user") or ""),
                identity_file=str(item.get("identity_file") or ""),
                origin=str(item.get("origin") or "tui"),
            ))
        return out

    def add_or_touch(self, dest: Destination) -> None:
        """Insert or move-to-front. Persists to disk after the
        update so the next launch shows the same order."""
        items = self.list()
        key = self._key(dest)
        items = [d for d in items if self._key(d) != key]
        items.insert(0, dest)
        items = items[: self.MAX_ENTRIES]
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump([asdict(d) for d in items], f, indent=2)


# ---------------------------------------------------------------------------
# Session credential cache (memory only)
# ---------------------------------------------------------------------------


class CredentialCache:
    """Process-lifetime cache for sensitive credentials.

    Two stores keyed by destination identity:

    - encryption passwords (used to decrypt keysets)
    - SFTP authentication payload (password or identity-file path)

    Nothing here is written to disk. Cache is dropped when the app
    process exits.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enc: Dict[tuple, str] = {}
        self._sftp: Dict[tuple, Dict[str, str]] = {}

    @staticmethod
    def _key(dest: Destination) -> tuple:
        return (dest.kind, dest.host, dest.port, dest.user, dest.path)

    def get_encryption_password(self, dest: Destination) -> Optional[str]:
        with self._lock:
            return self._enc.get(self._key(dest))

    def set_encryption_password(
        self, dest: Destination, password: str,
    ) -> None:
        with self._lock:
            self._enc[self._key(dest)] = password

    def get_sftp_auth(self, dest: Destination) -> Optional[Dict[str, str]]:
        with self._lock:
            cached = self._sftp.get(self._key(dest))
            return dict(cached) if cached else None

    def set_sftp_auth(
        self, dest: Destination, auth: Dict[str, str],
    ) -> None:
        with self._lock:
            self._sftp[self._key(dest)] = dict(auth)

    def forget(self, dest: Destination) -> None:
        k = self._key(dest)
        with self._lock:
            self._enc.pop(k, None)
            self._sftp.pop(k, None)
