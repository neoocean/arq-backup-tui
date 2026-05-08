"""Plan-management CLI hosted alongside the TUI launcher.

When invoked with no arguments, ``python -m arq_tui`` opens the
Textual app. When invoked with a recognized subcommand
(``plans list`` / ``plans show <id-or-name>`` / ``plans delete
<id-or-name>``), this module handles it without spinning up the
GUI.

Plan editing is deliberately not exposed (per project decision) —
recreate via the wizard and delete the old file with this CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from .state import Plan, PlanRegistry


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arq-tui",
        description=(
            "Textual TUI for arq-backup-tui. With no arguments, "
            "launches the interactive app. Subcommands operate on "
            "the saved plan files under "
            "$XDG_CONFIG_HOME/arq-backup-tui/plans/."
        ),
    )
    p.add_argument(
        "--config-dir",
        type=Path, default=None,
        help="Override the config directory (defaults to "
             "$XDG_CONFIG_HOME/arq-backup-tui).",
    )
    sub = p.add_subparsers(dest="command")

    plans = sub.add_parser("plans", help="Manage saved plans.")
    plans_sub = plans.add_subparsers(dest="plans_command", required=True)

    plans_sub.add_parser("list", help="List every saved plan.")

    show = plans_sub.add_parser(
        "show", help="Print one plan as JSON.",
    )
    show.add_argument(
        "id_or_name",
        help="Plan UUID (case-insensitive prefix OK) or exact name.",
    )

    delete = plans_sub.add_parser(
        "delete", help="Remove a plan file from disk.",
    )
    delete.add_argument(
        "id_or_name",
        help="Plan UUID (exact match) or exact name.",
    )
    delete.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt.",
    )

    # Runs / activity sub-commands. These are headless equivalents
    # of the RunsMonitorScreen and let cron / ops scripts read the
    # state-file directory without spawning the TUI.
    runs = sub.add_parser(
        "runs",
        help="Inspect / cancel / GC the runs state-file dir.",
    )
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    runs_sub.add_parser(
        "ls", help="List active + recent runs.",
    )
    runs_show = runs_sub.add_parser(
        "show", help="Print one run as JSON.",
    )
    runs_show.add_argument(
        "run_id",
        help="Run UUID (exact filename stem of the state file).",
    )
    runs_cancel = runs_sub.add_parser(
        "cancel",
        help="Send SIGTERM to a run's writer PID for graceful "
             "cancellation.",
    )
    runs_cancel.add_argument(
        "run_id",
        help="Run UUID to cancel.",
    )
    runs_gc = runs_sub.add_parser(
        "gc",
        help="Remove state files for runs that finished more "
             "than --older-than-days ago (default 30).",
    )
    runs_gc.add_argument(
        "--older-than-days", type=int, default=30,
        help="Age cutoff in days (default 30).",
    )

    return p


def _resolve_plan(reg: PlanRegistry, q: str) -> Optional[Plan]:
    plans = reg.list_plans()
    q_lower = q.lower()
    # Exact UUID first
    for p in plans:
        if p.plan_id.lower() == q_lower:
            return p
    # Exact name
    for p in plans:
        if p.name == q:
            return p
    # UUID prefix
    matches = [p for p in plans if p.plan_id.lower().startswith(q_lower)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(p.plan_id for p in matches)
        print(
            f"error: ambiguous plan {q!r}; matches: {ids}",
            file=sys.stderr,
        )
        return None
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parsed = _build_parser().parse_args(argv)
    config_dir = parsed.config_dir
    reg = PlanRegistry(config_dir=config_dir)

    if parsed.command is None:
        # No subcommand → launch the TUI.
        from .app import run_app
        return run_app(config_dir=config_dir)

    if parsed.command == "plans":
        if parsed.plans_command == "list":
            plans = reg.list_plans()
            if not plans:
                print("(no plans)")
                return 0
            for p in plans:
                print(
                    f"{p.plan_id}  {p.name!r}  "
                    f"sources={len(p.sources)}  "
                    f"dest={p.destination_kind}  "
                    f"last_run={p.last_run_iso or 'never'}"
                )
            return 0

        if parsed.plans_command == "show":
            plan = _resolve_plan(reg, parsed.id_or_name)
            if plan is None:
                print(
                    f"error: plan not found: {parsed.id_or_name!r}",
                    file=sys.stderr,
                )
                return 2
            doc = {
                "plan_id": plan.plan_id,
                "name": plan.name,
                "sources": list(plan.sources),
                "destination_kind": plan.destination_kind,
                "destination": dict(plan.destination),
                "chunker": plan.chunker,
                "use_packs": plan.use_packs,
                "dedup_against_existing": plan.dedup_against_existing,
                "exclude_globs": list(plan.exclude_globs),
                "exclude_regexes": list(plan.exclude_regexes),
                "exclude_gitignore_lines": list(
                    plan.exclude_gitignore_lines
                ),
                "max_file_bytes": plan.max_file_bytes,
                "use_apfs_snapshot": plan.use_apfs_snapshot,
                "retention": dict(plan.retention),
                "last_run_iso": plan.last_run_iso,
            }
            print(json.dumps(doc, indent=2, ensure_ascii=False))
            return 0

        if parsed.plans_command == "delete":
            plan = _resolve_plan(reg, parsed.id_or_name)
            if plan is None:
                print(
                    f"error: plan not found: {parsed.id_or_name!r}",
                    file=sys.stderr,
                )
                return 2
            if not parsed.yes:
                # Confirm by reading a y/n on stdin.
                ans = input(
                    f"Delete plan {plan.name!r} ({plan.plan_id})? [y/N] ",
                ).strip().lower()
                if ans not in ("y", "yes"):
                    print("aborted")
                    return 0
            removed = reg.delete(plan.plan_id)
            if removed:
                print(f"removed {plan.plan_id}")
                return 0
            print(
                f"error: file already gone for {plan.plan_id}",
                file=sys.stderr,
            )
            return 2

    if parsed.command == "runs":
        return _handle_runs_command(parsed)

    return 2


def _handle_runs_command(parsed) -> int:
    """Dispatch ``arq-tui runs <subcommand>`` — the headless
    equivalent of :class:`~arq_tui.screens.runs_monitor.RunsMonitorScreen`.
    Designed for cron-driven monitoring scripts and the operator's
    shell history.
    """
    from .runs import (
        RunStatus,
        enumerate_runs,
        gc_finished_runs,
        signal_cancel,
        state_file_path,
    )
    sub = parsed.runs_command
    if sub == "ls":
        recs = enumerate_runs()
        if not recs:
            print("(no runs)")
            return 0
        for rec in recs:
            tag = rec.status
            name = rec.plan_name or rec.run_id[:12]
            extra = ""
            if rec.progress.bytes_total:
                pct = (
                    100.0 * rec.progress.bytes_done
                    / max(rec.progress.bytes_total, 1)
                )
                extra = f"  {pct:5.1f}%"
            print(
                f"{rec.run_id}  {rec.kind:<8s}  {tag:<10s}  "
                f"{name}{extra}"
            )
        return 0
    if sub == "show":
        path = state_file_path(parsed.run_id)
        if not path.is_file():
            print(f"error: no such run: {parsed.run_id}",
                  file=sys.stderr)
            return 2
        print(path.read_text(encoding="utf-8"))
        return 0
    if sub == "cancel":
        for rec in enumerate_runs():
            if rec.run_id != parsed.run_id:
                continue
            if signal_cancel(rec):
                print(f"signaled SIGTERM to pid {rec.pid}")
                return 0
            print(
                f"error: pid {rec.pid} not alive (or no permission)",
                file=sys.stderr,
            )
            return 4
        print(f"error: no such run: {parsed.run_id}", file=sys.stderr)
        return 2
    if sub == "gc":
        n = gc_finished_runs(
            older_than_sec=parsed.older_than_days * 86400,
        )
        print(f"removed {n} state file(s)")
        return 0
    return 2
