"""Read-only adapter for a locally-installed Arq 7 (Arq.app) instance.

When the operator already runs Arq.app on this machine, the TUI should
mirror exactly what the Arq GUI shows — the same backup destinations,
the same backup plans, and the same activity log — so the two feel
nearly synchronised. When only one of the two tools is present each
keeps working independently.

Arq's backup agent keeps all of this in a single SQLite database at
``/Library/Application Support/ArqAgent/server.db`` (root-owned but
world-**readable**). The three tables we mirror:

- ``storage_locations`` → backup destinations (folder / sftp / cloud).
- ``backup_plans``       → backup plans. The per-row ``json`` column is
  the *same* 47-key ``backupplan.json`` schema this project already
  reads/writes, with ``backupFolderPlansByUUID`` carrying the source
  folders.
- ``activities``         → the activity log (every backup / restore /
  validate / delete run, with progress + finish state).

**Strictly read-only.** The agent owns ``server.db`` and writes to it
live; the file is also root-owned so we couldn't write it without sudo
even if we wanted to. We open a read-only connection (falling back to a
private snapshot copy if the agent holds a write lock) and never mutate
anything. Secrets never appear here either: encryption / SFTP passwords
live in the macOS Keychain + root-only sidecar files, so the DB only
carries a ``hasPassword`` boolean. Acting on a mirrored plan therefore
still goes through the normal session password prompt.

The mapping helpers (:meth:`ArqPlan.to_plan`,
:meth:`ArqStorageLocation.to_destination`) emit the TUI's own
:class:`~arq_tui.state.Plan` / :class:`~arq_tui.state.Destination`
dataclasses tagged with ``origin="arq"`` so the home screen can badge
them and refuse to overwrite them.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .state import Destination, Plan

# Standard on-disk locations for an Arq 7 install on macOS. Overridable
# (mainly so tests can point at a synthetic fixture DB).
DEFAULT_APP_PATH = Path("/Applications/Arq.app")
DEFAULT_SERVER_DB = Path(
    "/Library/Application Support/ArqAgent/server.db"
)
DEFAULT_LOGS_DIR = Path(
    "/Library/Application Support/ArqAgent/logs"
)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _folder_local_root(path: str) -> str:
    """Resolve a ``folder`` storage-location path to a local fs path.

    Arq encodes a folder destination as ``<mountpoint>:<relpath>`` —
    e.g. ``/Volumes/arqbackup1:/`` (mount ``/Volumes/arqbackup1``,
    relative ``/``) or ``/:Users/me/dest`` (mount ``/``, relative
    ``Users/me/dest``). The destination root our reader/writer wants is
    the mountpoint joined with the relative part.
    """
    if not path:
        return ""
    if ":" in path:
        mount, rel = path.split(":", 1)
        rel = rel.lstrip("/")
        if not rel:
            return mount or "/"
        return str(Path(mount or "/") / rel)
    return path


# ---------------------------------------------------------------------------
# Storage locations (destinations)
# ---------------------------------------------------------------------------


@dataclass
class ArqStorageLocation:
    """One row of Arq's ``storage_locations`` table (the ``json`` blob,
    decoded). Only non-secret fields are kept."""

    sl_id: int
    uuid: str
    name: str
    provider_type: str   # "folder" | "sftp" | "arqpremium" | "s3" | …
    plan_type: str = "backup"
    path: str = ""
    hostname: str = ""
    port: int = 0
    username: str = ""
    use_ssl: bool = False
    active: bool = False
    has_password: bool = False
    display_description: str = ""

    # Provider types the TUI's own backends can open directly. Cloud
    # providers (arqpremium / s3 / wasabi / …) are deliberately out of
    # scope (README §1) — we still *list* them so the operator sees the
    # full Arq picture, but ``to_destination`` returns ``None``.
    OPENABLE = ("folder", "sftp")

    @classmethod
    def from_row(
        cls, sl_id: int, raw_json: Optional[str],
    ) -> Optional["ArqStorageLocation"]:
        if not raw_json:
            return None
        try:
            j = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(j, dict):
            return None
        return cls(
            sl_id=int(j.get("id") or sl_id),
            uuid=str(j.get("uuid") or ""),
            name=str(j.get("name") or ""),
            provider_type=str(j.get("providerType") or ""),
            plan_type=str(j.get("planType") or "backup"),
            path=str(j.get("path") or ""),
            hostname=str(j.get("hostname") or ""),
            port=int(j.get("port") or 0),
            username=str(j.get("username") or ""),
            use_ssl=bool(j.get("useSSL", False)),
            active=bool(j.get("active", False)),
            has_password=bool(j.get("hasPassword", False)),
            display_description=str(j.get("displayDescription") or ""),
        )

    @property
    def is_openable(self) -> bool:
        return self.provider_type in self.OPENABLE

    def to_destination(self) -> Optional[Destination]:
        """Map to the TUI's :class:`Destination`, or ``None`` for a
        provider the local backends can't open (cloud)."""
        if self.provider_type == "folder":
            root = _folder_local_root(self.path)
            if not root:
                return None
            return Destination(
                kind="local", label=self.name or root, path=root,
                origin="arq",
            )
        if self.provider_type == "sftp":
            return Destination(
                kind="sftp",
                label=self.name or self.hostname,
                host=self.hostname,
                port=self.port or 22,
                user=self.username,
                path=self.path,
                origin="arq",
            )
        return None


# ---------------------------------------------------------------------------
# Backup plans
# ---------------------------------------------------------------------------


@dataclass
class ArqPlan:
    """One row of Arq's ``backup_plans`` table. ``raw`` is the full
    decoded ``json`` blob (same schema as our ``backupplan.json``)."""

    plan_uuid: str
    name: str
    storage_location_id: int
    active: bool = False
    last_backed_up: Optional[float] = None
    use_buzhash: bool = False
    use_apfs_snapshots: bool = False
    is_encrypted: bool = True
    raw: dict = field(default_factory=dict)
    storage_location: Optional[ArqStorageLocation] = None

    @classmethod
    def from_row(
        cls,
        *,
        plan_uuid: str,
        storage_location_id: int,
        active: bool,
        last_backed_up: Optional[float],
        raw_json: Optional[str],
    ) -> Optional["ArqPlan"]:
        try:
            j = json.loads(raw_json) if raw_json else {}
        except (json.JSONDecodeError, TypeError):
            j = {}
        if not isinstance(j, dict):
            j = {}
        return cls(
            plan_uuid=str(plan_uuid or j.get("planUUID") or ""),
            name=str(j.get("name") or ""),
            storage_location_id=int(
                storage_location_id or j.get("storageLocationId") or 0
            ),
            active=bool(active),
            last_backed_up=last_backed_up,
            use_buzhash=bool(j.get("useBuzhash", False)),
            use_apfs_snapshots=bool(j.get("useAPFSSnapshots", False)),
            is_encrypted=bool(j.get("isEncrypted", True)),
            raw=j,
        )

    # -- derived views over the raw json -----------------------------

    def folder_plans(self) -> List[dict]:
        """The per-source-folder plan dicts, sorted by name for a
        stable display order."""
        bfp = self.raw.get("backupFolderPlansByUUID") or {}
        if not isinstance(bfp, dict):
            return []
        out = [v for v in bfp.values() if isinstance(v, dict)]
        out.sort(key=lambda d: str(d.get("name") or "").lower())
        return out

    def sources(self) -> List[str]:
        """Absolute local source paths, one per backup folder."""
        out: List[str] = []
        for fp in self.folder_plans():
            lp = fp.get("localPath")
            if lp:
                out.append(str(lp))
        return out

    def _excludes(self) -> Dict[str, List[str]]:
        globs: List[str] = []
        regexes: List[str] = []
        skip_tm = False
        for fp in self.folder_plans():
            globs.extend(str(x) for x in fp.get("wildcardExcludes") or [])
            regexes.extend(str(x) for x in fp.get("regexExcludes") or [])
            if fp.get("skipTMExcludes"):
                skip_tm = True
        return {"globs": globs, "regexes": regexes, "skip_tm": skip_tm}

    def _retention(self) -> dict:
        """Map Arq's retain* fields onto our RetentionPolicy names.

        Arq: retainAll (bool) / retainHours / retainDays / retainWeeks
        / retainMonths. ``retainAll`` true = keep everything (empty
        policy). A zero in any bucket means "no limit for that band".
        """
        if self.raw.get("retainAll"):
            return {}
        out: dict = {}
        mapping = {
            "retainHours": "keep_hourly",
            "retainDays": "keep_daily",
            "retainWeeks": "keep_weekly",
            "retainMonths": "keep_monthly",
        }
        for arq_key, our_key in mapping.items():
            val = self.raw.get(arq_key)
            if isinstance(val, int) and val > 0:
                out[our_key] = val
        return out

    def to_plan(self) -> Plan:
        """Project this Arq plan onto the TUI's :class:`Plan`.

        ``plan_id`` is set to the planUUID so the destination top-level
        folder name (== planUUID in real Arq layouts) lines up when the
        plan is run through our writer. ``chunker`` follows Arq's
        ``useBuzhash`` toggle exactly as the writer's GAP-L logic does:
        Buzhash → ``arq_v7_41``; fixed → ``fixed-40m``.
        """
        ex = self._excludes()
        dest = (
            self.storage_location.to_destination()
            if self.storage_location is not None
            else None
        )
        if dest is not None and dest.kind == "sftp":
            destination_kind = "sftp"
            destination = {
                "host": dest.host,
                "port": dest.port,
                "user": dest.user,
                "path": dest.path,
            }
        else:
            destination_kind = "local"
            destination = {"path": dest.path if dest is not None else ""}
        return Plan(
            plan_id=self.plan_uuid,
            name=self.name,
            origin="arq",
            sources=self.sources(),
            destination_kind=destination_kind,
            destination=destination,
            chunker="arq_v7_41" if self.use_buzhash else "fixed-40m",
            use_packs=True,
            dedup_against_existing=True,
            exclude_globs=ex["globs"],
            exclude_regexes=ex["regexes"],
            skip_tm_excludes=ex["skip_tm"],
            use_apfs_snapshot=self.use_apfs_snapshots,
            retention=self._retention(),
            last_run_iso=_epoch_to_iso(self.last_backed_up),
        )


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


@dataclass
class ArqActivity:
    """One row of Arq's ``activities`` table (non-secret fields)."""

    uuid: str
    type: str            # "backup" | "restore" | "delete"
    sub_type: str        # "backup" | "validate" | "restore"
    message: str
    plan_uuid: Optional[str]
    processed_files: int = 0
    total_files: Optional[int] = None
    processed_bytes: int = 0
    total_bytes: Optional[int] = None
    created_time: float = 0.0
    finished_time: Optional[float] = None
    aborted: bool = False
    abort_reason: Optional[str] = None
    error_count: int = 0
    activity_log_path: Optional[str] = None

    @property
    def is_running(self) -> bool:
        """A run Arq still considers in-flight: not finished + not
        aborted. Mirrors the GUI's spinning rows."""
        return self.finished_time is None and not self.aborted

    @property
    def kind_label(self) -> str:
        """Short human label, e.g. ``backup`` / ``validate`` /
        ``restore`` — the validate sub_type rides on a backup row in
        Arq, so prefer the more specific sub_type when it differs."""
        if self.sub_type and self.sub_type != self.type:
            return self.sub_type
        return self.type

    def progress_fraction(self) -> Optional[float]:
        """Bytes-based completion in ``[0, 1]``, or ``None`` when the
        total isn't known yet (Arq leaves ``total_bytes`` NULL early in
        a run)."""
        if not self.total_bytes or self.total_bytes <= 0:
            return None
        return max(0.0, min(1.0, self.processed_bytes / self.total_bytes))


# ---------------------------------------------------------------------------
# The source
# ---------------------------------------------------------------------------


def _epoch_to_iso(epoch: Optional[float]) -> str:
    if not epoch:
        return ""
    from datetime import datetime, timezone
    try:
        return (
            datetime.fromtimestamp(float(epoch), timezone.utc)
            .isoformat(timespec="seconds")
        )
    except (ValueError, OverflowError, OSError):
        return ""


class _AttrConnection(sqlite3.Connection):
    """``sqlite3.Connection`` subclass that permits one bookkeeping
    attribute. The base C type has no ``__dict__``, so we can't tag a
    plain connection with its snapshot temp-dir for later cleanup — a
    trivial subclass restores attribute assignment."""

    _arq_tui_tmp_dir: Optional[str] = None


class ArqAppSource:
    """Read-only handle on a local Arq.app's ``server.db``.

    Construct via :func:`detect_arq_app` (which checks the app + DB are
    present) or directly with an explicit ``server_db`` path in tests.
    Every query opens its own short-lived read-only connection so the
    view stays live as the agent writes — callers that poll (the
    activity screen) simply re-query.
    """

    def __init__(
        self,
        server_db: os.PathLike | str,
        *,
        app_path: Optional[os.PathLike | str] = None,
        logs_dir: Optional[os.PathLike | str] = None,
    ) -> None:
        self.server_db = Path(server_db)
        self.app_path = Path(app_path) if app_path else DEFAULT_APP_PATH
        self.logs_dir = Path(logs_dir) if logs_dir else DEFAULT_LOGS_DIR

    # -- connection --------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a read-only connection.

        Preferred path: direct ``mode=ro`` against the live file (the
        DB uses ``journal_mode=delete``, so concurrent readers are
        safe; a short busy-timeout rides out the agent's brief write
        locks). If that still raises a locking/operational error, fall
        back to a private snapshot copy so a busy agent can never block
        the TUI from showing its data.
        """
        uri = f"file:{self.server_db}?mode=ro"
        try:
            con = sqlite3.connect(
                uri, uri=True, timeout=3.0, factory=_AttrConnection,
            )
            con.execute("PRAGMA busy_timeout=3000")
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.OperationalError:
            return self._connect_snapshot()

    def _connect_snapshot(self) -> sqlite3.Connection:
        tmp_dir = tempfile.mkdtemp(prefix="arq-tui-serverdb-")
        copy = Path(tmp_dir) / "server.db"
        shutil.copy2(self.server_db, copy)
        con = sqlite3.connect(
            f"file:{copy}?mode=ro", uri=True, factory=_AttrConnection,
        )
        con.row_factory = sqlite3.Row
        # The temp copy is cleaned when the connection is closed via
        # this attribute hook in :meth:`_close`.
        con._arq_tui_tmp_dir = tmp_dir
        return con

    @staticmethod
    def _close(con: sqlite3.Connection) -> None:
        tmp = getattr(con, "_arq_tui_tmp_dir", None)
        try:
            con.close()
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _has_column(con: sqlite3.Connection, table: str, col: str) -> bool:
        try:
            cols = {
                r[1] for r in con.execute(
                    f"PRAGMA table_info('{table}')"
                )
            }
        except sqlite3.Error:
            return False
        return col in cols

    # -- queries -----------------------------------------------------

    def storage_locations(
        self, *, active_only: bool = False,
    ) -> List[ArqStorageLocation]:
        """Every storage location Arq knows about, newest-touched
        first. Rows whose ``json`` blob won't decode are skipped."""
        try:
            con = self._connect()
        except sqlite3.Error:
            return []
        try:
            if not self._has_column(con, "storage_locations", "json"):
                return []
            where = "WHERE active = 1" if active_only else ""
            rows = con.execute(
                f"SELECT id, json FROM storage_locations {where} "
                "ORDER BY active DESC, id"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            self._close(con)
        out: List[ArqStorageLocation] = []
        for r in rows:
            sl = ArqStorageLocation.from_row(r["id"], r["json"])
            if sl is not None and sl.provider_type:
                out.append(sl)
        return out

    def plans(self, *, active_only: bool = False) -> List[ArqPlan]:
        """Every backup plan, each resolved against its storage
        location. Sorted by name (case-insensitive)."""
        sls = {sl.sl_id: sl for sl in self.storage_locations()}
        try:
            con = self._connect()
        except sqlite3.Error:
            return []
        try:
            if not self._has_column(con, "backup_plans", "json"):
                return []
            where = "WHERE active = 1" if active_only else ""
            rows = con.execute(
                "SELECT plan_uuid, storage_location_id, active, "
                f"last_backed_up, json FROM backup_plans {where}"
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            self._close(con)
        out: List[ArqPlan] = []
        for r in rows:
            p = ArqPlan.from_row(
                plan_uuid=r["plan_uuid"],
                storage_location_id=r["storage_location_id"],
                active=bool(r["active"]),
                last_backed_up=r["last_backed_up"],
                raw_json=r["json"],
            )
            if p is None:
                continue
            p.storage_location = sls.get(p.storage_location_id)
            out.append(p)
        out.sort(key=lambda pl: pl.name.lower())
        return out

    def activities(
        self,
        *,
        limit: int = 100,
        plan_uuid: Optional[str] = None,
        running_only: bool = False,
    ) -> List[ArqActivity]:
        """Activity-log rows, most-recent first.

        ``plan_uuid`` filters to a single plan; ``running_only`` keeps
        only in-flight runs (not finished, not aborted) for the
        live-activity view.
        """
        try:
            con = self._connect()
        except sqlite3.Error:
            return []
        clauses = []
        params: list = []
        if plan_uuid:
            clauses.append("plan_uuid = ?")
            params.append(plan_uuid)
        if running_only:
            clauses.append("finished_time IS NULL AND aborted = 0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            rows = con.execute(
                "SELECT uuid, type, sub_type, message, plan_uuid, "
                "processed_files, total_files, processed_bytes, "
                "total_bytes, created_time, finished_time, aborted, "
                "abort_reason, error_count, activity_log_path "
                f"FROM activities {where} "
                "ORDER BY created_time DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            self._close(con)
        out: List[ArqActivity] = []
        for r in rows:
            out.append(ArqActivity(
                uuid=str(r["uuid"]),
                type=str(r["type"] or ""),
                sub_type=str(r["sub_type"] or ""),
                message=str(r["message"] or ""),
                plan_uuid=r["plan_uuid"],
                processed_files=int(r["processed_files"] or 0),
                total_files=r["total_files"],
                processed_bytes=int(r["processed_bytes"] or 0),
                total_bytes=r["total_bytes"],
                created_time=float(r["created_time"] or 0.0),
                finished_time=r["finished_time"],
                aborted=bool(r["aborted"]),
                abort_reason=r["abort_reason"],
                error_count=int(r["error_count"] or 0),
                activity_log_path=r["activity_log_path"],
            ))
        return out


def detect_arq_app(
    *,
    server_db: Optional[os.PathLike | str] = None,
    app_path: Optional[os.PathLike | str] = None,
    require_app: bool = True,
) -> Optional[ArqAppSource]:
    """Return an :class:`ArqAppSource` if a local Arq.app install is
    usable, else ``None`` (the common case on a machine without Arq).

    Detection requires both the app bundle (``require_app``, on by
    default) and a readable ``server.db``. Tests pass an explicit
    ``server_db`` + ``require_app=False`` to use a synthetic fixture.
    """
    db = Path(server_db) if server_db else DEFAULT_SERVER_DB
    app = Path(app_path) if app_path else DEFAULT_APP_PATH
    if require_app and not app.exists():
        return None
    try:
        if not (db.is_file() and os.access(db, os.R_OK)):
            return None
    except OSError:
        return None
    return ArqAppSource(db, app_path=app)
