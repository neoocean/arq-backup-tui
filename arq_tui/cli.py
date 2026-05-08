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

    return 2
