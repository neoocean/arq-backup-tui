"""Source-machine identification for an Arq 7 destination.

Given just the destination's directory layout (no local Arq.app
state), this module extracts every metadata field Arq.app records
about the source machine — computer UUID, computer name, OS type
+ version, source path roots, disk identifier, volume name — and
optionally compares them to the current host so the operator can
answer "did THIS machine produce the backup?".

The comparison is metadata-based, NOT cryptographic: matching
fields are strong circumstantial evidence but don't formally
prove hardware identity. The cryptographic anchor is whether the
encryption password successfully decrypts the keyset (see
:func:`arq_validator.crypto.decrypt_keyset`); that proves the
operator possesses the key, not that the original backup came
from this exact box.

See ``docs/COMPATIBILITY.md`` for the underlying invariants the
metadata fields are pulled from.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from .backend import Backend
from .layout import discover_layout


@dataclass
class SourceMachineInfo:
    """Metadata extracted from one ``<computer-uuid>/`` subtree.

    ``raw_config`` / ``raw_plan`` carry the full sidecar JSON so
    callers can render every field (including ones we don't
    expose as named attributes); the named attributes cover the
    fields most useful for source-machine identification.
    """

    computer_uuid: str = ""
    computer_name: str = ""
    os_type: int = 0          # 1 = macOS, 2 = Windows
    os_version: str = ""
    arq_version: str = ""
    plan_id: str = ""
    plan_name: str = ""
    folder_count: int = 0
    raw_config: dict = field(default_factory=dict)
    raw_plan: dict = field(default_factory=dict)


@dataclass
class HostInfo:
    """Local-host metadata captured for comparison."""

    hostname: str = ""
    computer_name: str = ""   # macOS: scutil --get ComputerName
    username: str = ""
    os_name: str = ""         # uname -s
    os_version: str = ""      # macOS: sw_vers -productVersion


@dataclass
class MachineMatch:
    """Diff result of a :class:`SourceMachineInfo` against a
    :class:`HostInfo`. Each ``str`` field is one of:

    - ``"match"`` — values agreed (case-insensitive)
    - ``"differ"`` — both populated but disagree
    - ``"unknown"`` — at least one side missing
    """

    computer_name: str = "unknown"
    os_type: str = "unknown"
    os_version: str = "unknown"

    def is_strong_match(self) -> bool:
        """All fields either match or unknown — at least one
        actually matches. Returns False if any field outright
        ``differ``."""
        any_match = (
            self.computer_name == "match"
            or self.os_type == "match"
            or self.os_version == "match"
        )
        any_differ = (
            self.computer_name == "differ"
            or self.os_type == "differ"
            or self.os_version == "differ"
        )
        return any_match and not any_differ


# ---------------------------------------------------------------------------
# Read source-side metadata from a destination
# ---------------------------------------------------------------------------


def read_source_info(
    backend: Backend, root: str = "/",
) -> List[SourceMachineInfo]:
    """Return one :class:`SourceMachineInfo` per discovered
    computer subtree. Each entry's ``raw_config`` / ``raw_plan``
    carry the full sidecar JSON so callers can pretty-print or
    inspect any field. ``backend`` need not be open as a context
    manager — the caller is responsible for lifecycle."""
    out: List[SourceMachineInfo] = []
    for layout in discover_layout(
        backend, root, enumerate_objects=False,
    ):
        cu = layout.computer_uuid
        info = SourceMachineInfo(computer_uuid=cu)
        info.folder_count = len(layout.backup_folder_uuids)
        # backupconfig.json — Arq.app's per-computer settings.
        try:
            blob = backend.read_all(f"/{cu}/backupconfig.json")
            cfg = json.loads(blob.decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            cfg = {}
        info.raw_config = cfg
        info.computer_name = (
            cfg.get("computerName") or cfg.get("computerHostName") or ""
        )
        info.os_type = int(cfg.get("computerOSType") or 0)
        # backupplan.json — additional metadata (osVersion is here).
        try:
            blob = backend.read_all(f"/{cu}/backupplan.json")
            plan = json.loads(blob.decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            plan = {}
        info.raw_plan = plan
        info.plan_id = plan.get("planUUID") or plan.get("uuid") or ""
        info.plan_name = (
            plan.get("name") or plan.get("backupPlanName") or ""
        )
        info.arq_version = plan.get("arqVersion") or ""
        info.os_version = (
            plan.get("osVersion")
            or plan.get("computerOSVersion")
            or ""
        )
        out.append(info)
    return out


# ---------------------------------------------------------------------------
# Read this host's metadata
# ---------------------------------------------------------------------------


def read_host_info() -> HostInfo:
    """Capture the current host's identifying metadata. POSIX-only
    in practice; Windows lookups fall through to defaults."""
    info = HostInfo()
    try:
        info.hostname = socket.gethostname()
    except OSError:
        pass
    try:
        info.username = os.environ.get("USER") or os.environ.get(
            "USERNAME"
        ) or ""
    except OSError:
        pass
    try:
        uname = os.uname()
        info.os_name = uname.sysname
    except (AttributeError, OSError):
        pass
    # macOS-specific richer signals: ComputerName + sw_vers.
    if info.os_name == "Darwin":
        try:
            cp = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                capture_output=True, timeout=5,
            )
            if cp.returncode == 0:
                info.computer_name = cp.stdout.decode().strip()
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            cp = subprocess.run(
                ["sw_vers", "-productVersion"],
                capture_output=True, timeout=5,
            )
            if cp.returncode == 0:
                info.os_version = cp.stdout.decode().strip()
        except (OSError, subprocess.SubprocessError):
            pass
    if not info.computer_name:
        info.computer_name = info.hostname.split(".", 1)[0]
    return info


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


def _cmp_text(a: str, b: str) -> str:
    if not a or not b:
        return "unknown"
    if a.strip().lower() == b.strip().lower():
        return "match"
    return "differ"


def compare(
    source: SourceMachineInfo, host: HostInfo,
) -> MachineMatch:
    """Per-field comparison of source-side metadata against the
    current host. Returns a :class:`MachineMatch` summarizing the
    overlap.

    Comparisons are case-insensitive and tolerant of empty
    strings — missing data on either side counts as ``unknown``,
    not a difference."""
    match = MachineMatch()
    match.computer_name = _cmp_text(source.computer_name, host.computer_name)
    # OS type comparison: 1=macOS=Darwin, 2=Windows, etc.
    if not source.os_type or not host.os_name:
        match.os_type = "unknown"
    elif source.os_type == 1 and host.os_name == "Darwin":
        match.os_type = "match"
    elif source.os_type == 2 and host.os_name in (
        "Windows", "Windows_NT",
    ):
        match.os_type = "match"
    elif source.os_type == 3 and host.os_name == "Linux":
        # Per Arq 7 spec, 3 = Linux (uncommon but exists).
        match.os_type = "match"
    else:
        match.os_type = "differ"
    match.os_version = _cmp_text(source.os_version, host.os_version)
    return match
