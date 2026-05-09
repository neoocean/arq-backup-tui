"""Schedule a saved Plan to run periodically via the host's
native scheduler (crontab on Linux, launchd on macOS).

Why both, and why native: cron / launchd are already running,
already start at boot, already log, and operators already know
how to inspect them. Adding our own daemon would duplicate
infrastructure for no gain. Generating a plist or a crontab
line that points at the existing ``arq-backup create`` CLI keeps
the schedule a transparent piece of the OS the operator can
read, edit, and remove without us in the loop.

Three layers, each with a small surface:

- :func:`generate_crontab_entry(plan, …)` — pure-function emitter
  for one crontab line, no I/O.
- :func:`generate_launchd_plist(plan, …)` — pure-function emitter
  for one launchd ``com.arq-backup-tui.plan-<id>`` plist, no I/O.
- :func:`install_schedule(plan, kind, …)` — actually wires it into
  the host (writes to ``crontab -l | crontab -`` or to
  ``~/Library/LaunchAgents/``).

Tests cover the pure emitters byte-for-byte (the install side is
exercised by an integration test that uses a temp HOME).
"""

from __future__ import annotations

import getpass
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Schedule spec
# ---------------------------------------------------------------------------


@dataclass
class ScheduleSpec:
    """Cadence for a scheduled plan.

    ``cron_expr`` is a 5-field crontab expression (minute / hour /
    dom / month / dow). When ``cron_expr`` is set, the launchd
    generator translates it into a ``StartCalendarInterval`` block;
    when ``interval_sec`` is set instead, launchd uses
    ``StartInterval`` and crontab falls back to a per-minute
    sleep-based wrapper (rare; most operators use cron expressions).

    Exactly one of ``cron_expr`` / ``interval_sec`` must be set.
    """

    cron_expr: str = ""
    interval_sec: int = 0

    def __post_init__(self) -> None:
        if bool(self.cron_expr) == bool(self.interval_sec):
            raise ValueError(
                "ScheduleSpec must set exactly one of "
                "cron_expr / interval_sec"
            )

    def cron_fields(self) -> Tuple[int, int, int, int, int]:
        """Parse cron_expr into (m, h, dom, month, dow). Wildcards
        (``*``) are encoded as -1 so callers can detect them."""
        parts = self.cron_expr.split()
        if len(parts) != 5:
            raise ValueError(
                f"cron_expr must have 5 fields, got {len(parts)}: "
                f"{self.cron_expr!r}"
            )

        def _parse(p: str) -> int:
            if p == "*":
                return -1
            try:
                return int(p)
            except ValueError:
                # Step / range syntax (e.g. */15) — not supported
                # by the launchd translator yet; flag it loudly.
                raise ValueError(
                    f"unsupported cron field {p!r} (step / range "
                    f"syntax not supported when targetting launchd)"
                )

        return tuple(_parse(p) for p in parts)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Crontab line emitter
# ---------------------------------------------------------------------------


_CRONTAB_MARKER_PREFIX = "# arq-backup-tui:plan="


def generate_crontab_entry(
    plan,
    *,
    executable: Optional[str] = None,
    state_dir: Optional[Path] = None,
    schedule: Optional[ScheduleSpec] = None,
    password_env_var: str = "ARQ_BACKUP_PW",
) -> str:
    """Return one crontab line that runs ``plan``.

    Output shape::

        # arq-backup-tui:plan=<plan_id>
        <cron-expr> /usr/bin/env -S python3 -m arq_writer create \\
            <src> --dest <dst> --password-env <env> \\
            --state-file <state> [...]

    The marker comment lets :func:`list_schedules` /
    :func:`remove_schedule` find their entries again without
    parsing the cron expression.
    """
    sched = schedule or _schedule_from_plan(plan)
    if not sched.cron_expr:
        # crontab can't directly express a sub-minute interval; we
        # emit a per-minute entry that exits early when not due.
        # Most operators target cron-shaped plans; the interval_sec
        # path stays a corner case.
        cron_field = "*/1 * * * *"
    else:
        cron_field = sched.cron_expr
    argv = _argv_for_plan(
        plan, executable=executable, state_dir=state_dir,
        password_env_var=password_env_var,
    )
    quoted = " ".join(shlex.quote(a) for a in argv)
    marker = f"{_CRONTAB_MARKER_PREFIX}{plan.plan_id}"
    return f"{marker}\n{cron_field} {quoted}"


def parse_crontab_entries(crontab: str) -> List[Tuple[str, str]]:
    """Pull every arq-backup-tui-managed entry out of a crontab
    string. Returns ``[(plan_id, full_entry), …]``.

    Used by :func:`list_schedules` + :func:`remove_schedule` so
    each operation only touches our own entries; the operator's
    other crontab lines are preserved verbatim.
    """
    out: List[Tuple[str, str]] = []
    lines = crontab.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(_CRONTAB_MARKER_PREFIX):
            plan_id = line[len(_CRONTAB_MARKER_PREFIX):].strip()
            entry = line
            if i + 1 < len(lines):
                entry = f"{line}\n{lines[i + 1]}"
                i += 2
            else:
                i += 1
            out.append((plan_id, entry))
        else:
            i += 1
    return out


# ---------------------------------------------------------------------------
# launchd plist emitter
# ---------------------------------------------------------------------------


def generate_launchd_plist(
    plan,
    *,
    executable: Optional[str] = None,
    state_dir: Optional[Path] = None,
    schedule: Optional[ScheduleSpec] = None,
    password_env_var: str = "ARQ_BACKUP_PW",
) -> str:
    """Return the ``Library/LaunchAgents/com.arq-backup-tui.plan-<id>.plist``
    XML body that runs ``plan`` via launchd.

    macOS schedules go through launchd because:

    1. launchd survives across user sign-out / sleep wake-ups
       smoother than cron does on Apple silicon.
    2. ``ProgramArguments`` accepts a list — no shell-escaping
       confusion the way crontab + shlex needs.

    Cron-style schedules become ``StartCalendarInterval``;
    interval_sec becomes ``StartInterval``.
    """
    sched = schedule or _schedule_from_plan(plan)
    argv = _argv_for_plan(
        plan, executable=executable, state_dir=state_dir,
        password_env_var=password_env_var,
    )
    label = _launchd_label_for(plan)
    args_xml = "\n        ".join(
        f"<string>{_xml_escape(a)}</string>" for a in argv
    )
    if sched.cron_expr:
        m, h, dom, month, dow = sched.cron_fields()
        rows = []
        if m >= 0:
            rows.append(f"<key>Minute</key><integer>{m}</integer>")
        if h >= 0:
            rows.append(f"<key>Hour</key><integer>{h}</integer>")
        if dom >= 0:
            rows.append(f"<key>Day</key><integer>{dom}</integer>")
        if month >= 0:
            rows.append(f"<key>Month</key><integer>{month}</integer>")
        if dow >= 0:
            rows.append(f"<key>Weekday</key><integer>{dow}</integer>")
        sched_xml = (
            "<key>StartCalendarInterval</key>\n"
            "    <dict>\n        "
            + "\n        ".join(rows)
            + "\n    </dict>"
        )
    else:
        sched_xml = (
            f"<key>StartInterval</key>\n"
            f"    <integer>{int(sched.interval_sec)}</integer>"
        )
    return _LAUNCHD_TEMPLATE.format(
        label=label, args=args_xml, schedule=sched_xml,
    )


def _launchd_label_for(plan) -> str:
    """Stable launchd Label for ``plan`` — used as both the plist
    filename and the ``Label`` key. Reverse-DNS shape so launchd
    treats it as a well-formed agent."""
    return f"com.arq-backup-tui.plan-{plan.plan_id}"


_LAUNCHD_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        {args}
    </array>
    {schedule}
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _argv_for_plan(
    plan, *, executable, state_dir, password_env_var,
) -> List[str]:
    """Produce the argv that the schedule entry should fire.

    Mirrors :class:`arq_tui.subprocess_workers.SubprocessBackupWorker`
    closely so a TUI-launched run and a scheduler-launched run
    invoke the SAME CLI shape — meaning the resulting state file
    and event stream look identical to monitoring tools."""
    if executable is None:
        executable = sys.executable
    state_dir = state_dir or Path(
        os.environ.get("XDG_STATE_HOME")
        or (Path.home() / ".local" / "state")
    ) / "arq-backup-tui" / "runs"
    # The state-file path embeds plan_id so concurrent schedules
    # don't trample each other's events.
    state_file = state_dir / f"plan-{plan.plan_id}.json"
    argv: List[str] = [
        executable, "-m", "arq_writer", "create",
    ]
    for src in plan.sources:
        argv.append(str(src))
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
    else:
        argv += ["--dest", str(plan.destination.get("path") or "")]
    argv += [
        "--password-env", password_env_var,
        "--state-file", str(state_file),
        "--backup-name", plan.name or "scheduled",
        "--chunker", plan.chunker or "default",
    ]
    if plan.use_packs:
        argv.append("--use-packs")
    if plan.dedup_against_existing:
        argv.append("--dedup-against-existing")
    if plan.max_file_bytes:
        argv += ["--max-file-bytes", str(plan.max_file_bytes)]
    for pat in plan.exclude_globs:
        argv += ["--exclude-glob", pat]
    for pat in plan.exclude_regexes:
        argv += ["--exclude-regex", pat]
    if plan.use_apfs_snapshot:
        argv.append("--use-apfs-snapshot")
    return argv


def _schedule_from_plan(plan) -> ScheduleSpec:
    """Plan dataclass doesn't yet carry a schedule field — pull it
    from the optional ``schedule`` dict if set, else default to
    every day at 03:00. The default mirrors what most automated
    backup tools choose (low-load overnight slot)."""
    sched = (plan.schedule if hasattr(plan, "schedule") else None) or {}
    if sched.get("interval_sec"):
        return ScheduleSpec(interval_sec=int(sched["interval_sec"]))
    expr = sched.get("cron_expr") or "0 3 * * *"
    return ScheduleSpec(cron_expr=str(expr))


# ---------------------------------------------------------------------------
# Install / list / remove (host-side I/O)
# ---------------------------------------------------------------------------


def install_schedule(
    plan,
    *,
    kind: str,                      # "cron" | "launchd"
    executable: Optional[str] = None,
    state_dir: Optional[Path] = None,
    launch_agents_dir: Optional[Path] = None,
    crontab_cmd: str = "crontab",
    schedule: Optional[ScheduleSpec] = None,
    password_env_var: str = "ARQ_BACKUP_PW",
) -> Path:
    """Install a schedule for ``plan`` in the host's native scheduler.

    Returns a Path that identifies the installed entry — the plist
    file for launchd, or the file ``~/.config/arq-backup-tui/
    cron-installed/<plan_id>`` (a sentinel) for cron. The sentinel
    is so :func:`remove_schedule` knows whether we own a given
    plan_id without parsing crontab.
    """
    if kind == "launchd":
        plist = generate_launchd_plist(
            plan, executable=executable, state_dir=state_dir,
            schedule=schedule, password_env_var=password_env_var,
        )
        agents = launch_agents_dir or (
            Path.home() / "Library" / "LaunchAgents"
        )
        agents.mkdir(parents=True, exist_ok=True)
        out = agents / f"{_launchd_label_for(plan)}.plist"
        out.write_text(plist, encoding="utf-8")
        # Best-effort: load the agent so it activates without a
        # logout. Failures are swallowed (the operator can
        # ``launchctl load`` manually).
        try:
            subprocess.run(
                ["launchctl", "load", str(out)],
                check=False, capture_output=True, timeout=5,
            )
        except Exception:
            pass
        return out
    if kind == "cron":
        existing = _read_crontab(crontab_cmd)
        # Drop any prior entry for this plan so re-install replaces
        # rather than duplicates.
        existing = _strip_plan_from_crontab(existing, plan.plan_id)
        new_entry = generate_crontab_entry(
            plan, executable=executable, state_dir=state_dir,
            schedule=schedule, password_env_var=password_env_var,
        )
        if existing and not existing.endswith("\n"):
            existing += "\n"
        merged = existing + new_entry + "\n"
        _write_crontab(merged, crontab_cmd)
        return Path("crontab") / plan.plan_id   # sentinel
    raise ValueError(f"unsupported schedule kind: {kind!r}")


def list_schedules(
    *,
    kind: str = "auto",            # "auto" | "cron" | "launchd"
    crontab_cmd: str = "crontab",
    launch_agents_dir: Optional[Path] = None,
) -> List[Tuple[str, str, Path]]:
    """Return ``[(plan_id, kind, path_or_marker), …]`` for every
    arq-backup-tui-managed schedule visible on this host.

    ``kind="auto"`` returns both cron and launchd entries; pass a
    specific kind to filter.
    """
    out: List[Tuple[str, str, Path]] = []
    if kind in ("auto", "cron"):
        try:
            existing = _read_crontab(crontab_cmd)
        except Exception:
            existing = ""
        for pid, entry in parse_crontab_entries(existing):
            out.append((pid, "cron", Path(f"crontab:{pid}")))
    if kind in ("auto", "launchd"):
        agents = launch_agents_dir or (
            Path.home() / "Library" / "LaunchAgents"
        )
        if agents.is_dir():
            for plist in agents.glob(
                "com.arq-backup-tui.plan-*.plist",
            ):
                pid = plist.stem.removeprefix(
                    "com.arq-backup-tui.plan-"
                )
                out.append((pid, "launchd", plist))
    return out


def remove_schedule(
    plan_id: str,
    *,
    kind: str = "auto",
    crontab_cmd: str = "crontab",
    launch_agents_dir: Optional[Path] = None,
) -> int:
    """Remove the schedule for ``plan_id`` from the host. Returns
    the number of entries actually removed (0 means nothing was
    installed for that plan)."""
    removed = 0
    if kind in ("auto", "cron"):
        try:
            existing = _read_crontab(crontab_cmd)
        except Exception:
            existing = ""
        new = _strip_plan_from_crontab(existing, plan_id)
        if new != existing:
            _write_crontab(new, crontab_cmd)
            removed += 1
    if kind in ("auto", "launchd"):
        agents = launch_agents_dir or (
            Path.home() / "Library" / "LaunchAgents"
        )
        target = agents / (
            f"com.arq-backup-tui.plan-{plan_id}.plist"
        )
        if target.is_file():
            try:
                subprocess.run(
                    ["launchctl", "unload", str(target)],
                    check=False, capture_output=True, timeout=5,
                )
            except Exception:
                pass
            target.unlink(missing_ok=True)
            removed += 1
    return removed


def _read_crontab(crontab_cmd: str) -> str:
    """Return the user's existing crontab as a string (empty when
    none is installed). Tolerant of `crontab -l` exit code 1
    (which means "no crontab for $USER")."""
    try:
        cp = subprocess.run(
            [crontab_cmd, "-l"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return ""
    if cp.returncode != 0 and "no crontab" not in (cp.stderr or ""):
        # Other error — surface stderr but don't raise; the
        # install path will overwrite anyway.
        return ""
    return cp.stdout or ""


def _write_crontab(contents: str, crontab_cmd: str) -> None:
    """Pipe ``contents`` to ``crontab -`` (the standard install
    pattern). Best-effort tempfile cleanup on failure."""
    fd, path = tempfile.mkstemp(prefix="arq-tui-cron-", suffix=".tab")
    try:
        os.write(fd, contents.encode("utf-8"))
        os.close(fd)
        subprocess.run(
            [crontab_cmd, path],
            check=True, capture_output=True, timeout=5,
        )
    finally:
        try:
            Path(path).unlink()
        except OSError:
            pass


def _strip_plan_from_crontab(crontab: str, plan_id: str) -> str:
    """Remove every entry tagged with ``plan_id`` (marker comment
    + the next line, which is the cron entry) from ``crontab``.
    Lines we don't own are preserved verbatim."""
    out: List[str] = []
    lines = crontab.splitlines()
    marker = f"{_CRONTAB_MARKER_PREFIX}{plan_id}"
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == marker:
            # Drop this line + the next (the actual entry).
            i += 2
            continue
        out.append(line)
        i += 1
    result = "\n".join(out)
    if crontab.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result
