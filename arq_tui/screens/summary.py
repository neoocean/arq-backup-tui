"""Summary dashboard — a one-glance report of the whole backup estate.

The sidebar's Summary section renders this panel: backup plans, storage
locations, in-progress + recent activity (instant, from the Arq mirror +
local run state), plus per-destination depth (record count, oldest /
newest backup, on-disk size) gathered in a worker thread so the UI never
blocks. ``[s]`` saves the rendered report to a text file.
"""

from __future__ import annotations

import datetime
import os
import time
from pathlib import Path
from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from ..state import Plan

RECENT_WINDOW_SEC = 24 * 3600


def _fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if f < 1024 or unit == "PiB":
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{n} B"


def _fmt_epoch(epoch: Optional[float]) -> str:
    if not epoch:
        return "—"
    try:
        return datetime.datetime.fromtimestamp(
            float(epoch)
        ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "—"


def _dir_size(path: str) -> int:
    """Sum of file sizes under ``path`` (best-effort; skips unreadable)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


class SummaryPanel(Vertical):
    """Right-hand content for the sidebar's Summary section."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("s", "save_report", "Save report", show=True),
    ]

    DEFAULT_CSS = """
    SummaryPanel {
        padding: 1 2;
        height: 1fr;
    }
    SummaryPanel .title { text-style: bold; }
    SummaryPanel .hint { color: $text-muted; margin-bottom: 1; }
    SummaryPanel #summary-scroll {
        height: 1fr;
        border: round $surface-lighten-2;
        padding: 0 1;
    }
    """

    def __init__(self, *, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self._report_text = ""
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield Static("Summary", classes="title")
        yield Static(
            "[r] refresh    [s] save report", classes="hint", markup=False,
        )
        with VerticalScroll(id="summary-scroll"):
            yield Static("(gathering…)", id="summary-body", markup=False)

    def on_mount(self) -> None:
        # Render the instant part immediately, then enrich with the
        # worker-gathered per-destination depth.
        self._apply(self._base_report())
        self.action_refresh()

    # ------------------------------------------------------------------
    # Plan / destination gathering (no IO)
    # ------------------------------------------------------------------

    def _all_plans(self) -> List[Plan]:
        own = self.app.plan_registry.list_plans()
        arq_src = getattr(self.app, "arq_app", None)
        if arq_src is None:
            return own
        own_ids = {p.plan_id for p in own if p.plan_id}
        arq = [ap.to_plan() for ap in arq_src.plans(active_only=True)]
        return own + [p for p in arq if p.plan_id not in own_ids]

    def _base_report(self) -> str:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"Backup summary — generated {now}", ""]

        plans = self._all_plans()
        lines.append(f"Backup plans: {len(plans)}")
        for p in plans:
            origin = "Arq" if p.origin == "arq" else "own"
            last = p.last_run_iso or "never"
            lines.append(
                f"  • {p.name}  [{origin}]  sources={len(p.sources)}  "
                f"last backup={last}"
            )
        lines.append("")

        from .backup_sets import merged_destinations
        dests = merged_destinations(self.app)
        lines.append(f"Storage locations: {len(dests)}")
        for d in dests:
            lines.append(f"  • {d.display()}  ({d.kind})")
        lines.append("")

        # Activity: in-progress + recent counts (Arq mirror + own runs).
        arq_src = getattr(self.app, "arq_app", None)
        running, recent = [], []
        if arq_src is not None:
            try:
                running = arq_src.activities(running_only=True, limit=50)
                recent = arq_src.activities(limit=300)
            except Exception:
                running, recent = [], []
        lines.append(f"In progress: {len(running)}")
        for a in running:
            frac = a.progress_fraction()
            pct = f"{frac * 100:.0f}%" if frac is not None else "?"
            lines.append(f"  ▶ {a.kind_label}  {a.message}  {pct}")
        now_ts = time.time()
        n_ok = n_fail = n_restore = n_validate = 0
        for a in recent:
            ref = a.finished_time or a.created_time
            if not ref or (now_ts - ref) > RECENT_WINDOW_SEC:
                continue
            if a.kind_label == "restore":
                n_restore += 1
            elif a.kind_label == "validate":
                n_validate += 1
            elif a.error_count or a.aborted:
                n_fail += 1
            else:
                n_ok += 1
        lines.append(
            f"Recent (24h): {n_ok} backups ok, {n_fail} failed/aborted, "
            f"{n_restore} restores, {n_validate} validations"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Per-destination depth (worker thread)
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        if not self.display:
            return
        if self._refreshing:
            return
        self._refreshing = True
        self._apply(self._base_report() + "\n\nBackup sets: (reading…)")
        self.run_worker(
            self._gather_deep, thread=True, exclusive=True,
            group="summary-deep",
        )

    def _gather_deep(self) -> None:
        from .backup_sets import merged_destinations
        from arq_reader import Restore
        from ..backend_open import close_backend, open_backend
        lines = ["", "Backup sets:"]
        try:
            dests = merged_destinations(self.app)
        except Exception:
            dests = []
        for d in dests:
            pw = self.app.credential_cache.get_encryption_password(d)
            if d.kind != "local":
                lines.append(
                    f"  • {d.display()}: remote — open in Storage "
                    "Locations to include"
                )
                continue
            if not pw:
                lines.append(
                    f"  • {d.display()}: locked — open it in Storage "
                    "Locations (enter password) to include"
                )
                continue
            backend = None
            try:
                backend = open_backend(d)
                from arq_validator.layout import discover_layout
                layouts = discover_layout(backend, "/")
                rs = Restore("/", encryption_password=pw, backend=backend)
                total_records = 0
                oldest = None
                newest = None
                for lay in layouts:
                    for fu in lay.backup_folder_uuids:
                        try:
                            recs = rs.list_records(
                                folder_uuid=fu,
                                computer_uuid=lay.computer_uuid,
                            )
                        except Exception:
                            continue
                        total_records += len(recs)
                        for r in recs:
                            cd = r.creation_date or 0
                            if cd:
                                oldest = cd if oldest is None else min(oldest, cd)
                                newest = cd if newest is None else max(newest, cd)
                size = _dir_size(d.path)
                lines.append(
                    f"  • {d.display()}: {total_records} records, "
                    f"oldest={_fmt_epoch(oldest)}, newest={_fmt_epoch(newest)}, "
                    f"size={_fmt_bytes(size)}"
                )
            except Exception as exc:
                lines.append(f"  • {d.display()}: (error: {exc})")
            finally:
                if backend is not None:
                    close_backend(backend)
        deep = "\n".join(lines)
        self.app.call_from_thread(self._apply_deep, deep)

    def _apply_deep(self, deep: str) -> None:
        self._refreshing = False
        self._apply(self._base_report() + "\n" + deep)

    # ------------------------------------------------------------------
    # Render + save
    # ------------------------------------------------------------------

    def _apply(self, text: str) -> None:
        self._report_text = text
        try:
            self.query_one("#summary-body", Static).update(text)
        except Exception:
            pass

    def action_save_report(self) -> None:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        cfg = self.app.plan_registry.config_dir
        out_dir = Path(cfg) / "summaries"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"summary-{stamp}.txt"
            path.write_text(self._report_text, encoding="utf-8")
            self.notify(f"Saved report to {path}", severity="information")
        except OSError as exc:
            self.notify(f"Could not save report: {exc}", severity="error")
