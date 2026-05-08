"""Persistent state for the TUI.

For M1 only :class:`PlanRegistry` is wired — and it's intentionally
empty-by-default. M3 expands it with plan creation / load / save.

Storage layout (created on first save, see M3):

    $XDG_CONFIG_HOME/arq-backup-tui/
    ├── config.toml              # global settings (theme, etc.)
    ├── plans/
    │   └── <plan-uuid>.json     # one plan per file
    └── recent_destinations.json
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


def _default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(
        Path.home() / ".config"
    )
    return Path(base) / "arq-backup-tui"


@dataclass
class Plan:
    """A backup plan record. Most fields are populated by the M3
    plan wizard; M1 only models the shape."""

    plan_id: str = ""
    name: str = ""
    sources: List[str] = field(default_factory=list)
    destination_kind: str = "local"   # "local" | "sftp"
    destination: dict = field(default_factory=dict)
    chunker: str = "default"
    use_packs: bool = True
    dedup_against_existing: bool = True
    last_run_iso: str = ""


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
        import json
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
            out.append(Plan(
                plan_id=str(data.get("plan_id") or ""),
                name=str(data.get("name") or ""),
                sources=[
                    str(s) for s in data.get("sources") or []
                ],
                destination_kind=str(
                    data.get("destination_kind") or "local"
                ),
                destination=dict(data.get("destination") or {}),
                chunker=str(data.get("chunker") or "default"),
                use_packs=bool(data.get("use_packs", True)),
                dedup_against_existing=bool(
                    data.get("dedup_against_existing", True)
                ),
                last_run_iso=str(data.get("last_run_iso") or ""),
            ))
        out.sort(key=lambda pl: pl.name.lower())
        return out
