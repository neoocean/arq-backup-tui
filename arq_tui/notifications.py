"""Pluggable notification on backup completion / failure.

When a long-running backup finishes (success OR failure) the
operator usually isn't watching the TUI. This module fires a
configurable notification so they don't have to keep tabbing
back to check.

Three built-in handler classes:

- :class:`MacOSNotificationHandler` — invokes ``osascript`` to
  pop a Notification Center notification on macOS.
- :class:`LinuxNotifySendHandler` — invokes ``notify-send`` on
  Linux desktops with libnotify.
- :class:`ShellCommandHandler` — runs an operator-supplied
  shell command with the run details as env vars. Use this for
  Slack / Pushover / email integrations without baking those
  vendors into the codebase.

Plus the dispatcher :func:`notify_run_finished(record)` which
picks the right handler(s) based on platform + operator
configuration.

Configuration lives in ``~/.config/arq-backup-tui/
notifications.json`` (operator-managed). Empty / missing →
auto-detect by platform (mac → osascript, linux → notify-send,
nothing on others).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class NotificationConfig:
    """Operator-managed notification settings.

    Loaded from ``notifications.json`` if present; otherwise
    auto-detected per platform.
    """
    enabled: bool = True
    # When set, run on every status change. When empty, only
    # fire on FAILED (the most useful default — operators don't
    # need a desktop notification for every successful nightly
    # backup).
    on_status: List[str] = field(
        default_factory=lambda: ["failed", "cancelled"],
    )
    # macOS: use osascript. Linux: use notify-send. None = skip.
    desktop_kind: Optional[str] = None
    # Optional shell command run for every notification. The
    # run record is passed via environment vars + the command's
    # stdin (JSON). Useful for Slack/Pushover/email/etc.
    shell_command: Optional[str] = None


def load_config(
    path: Optional[Path] = None,
) -> NotificationConfig:
    """Load operator config from ``path`` or auto-detect."""
    if path is None:
        path = (
            Path(os.environ.get("XDG_CONFIG_HOME")
                 or Path.home() / ".config")
            / "arq-backup-tui"
            / "notifications.json"
        )
    if path.is_file():
        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
            )
            return NotificationConfig(
                enabled=bool(data.get("enabled", True)),
                on_status=list(
                    data.get("on_status")
                    or ["failed", "cancelled"],
                ),
                desktop_kind=data.get("desktop_kind"),
                shell_command=data.get("shell_command"),
            )
        except (OSError, ValueError):
            pass
    # Auto-detect.
    cfg = NotificationConfig()
    sys_name = platform.system()
    if sys_name == "Darwin" and shutil.which("osascript"):
        cfg.desktop_kind = "osascript"
    elif sys_name == "Linux" and shutil.which("notify-send"):
        cfg.desktop_kind = "notify-send"
    return cfg


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _send_macos(title: str, body: str) -> bool:
    if not shutil.which("osascript"):
        return False
    # osascript display notification expects double-quoted
    # strings; escape inner double quotes + backslashes.
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(title)}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _send_notify_send(title: str, body: str) -> bool:
    if not shutil.which("notify-send"):
        return False
    try:
        subprocess.run(
            ["notify-send", title, body],
            check=False, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _send_shell(
    cmd: str, env_payload: Dict[str, str], stdin_json: str,
) -> bool:
    """Run ``cmd`` with the run record exposed as env vars +
    JSON via stdin. Operator's command can read either."""
    full_env = dict(os.environ)
    for k, v in env_payload.items():
        full_env[f"ARQ_NOTIFY_{k.upper()}"] = v
    try:
        subprocess.run(
            cmd, shell=True,
            input=stdin_json, text=True,
            env=full_env, timeout=30,
            check=False, capture_output=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def notify_run_finished(
    record,
    *,
    config: Optional[NotificationConfig] = None,
) -> Dict[str, bool]:
    """Fire all configured notifications for a finished run.

    ``record`` is a :class:`arq_tui.runs.RunRecord` (the on-
    disk state-file shape). Returns a dict reporting which
    handlers actually ran (e.g. ``{"desktop": True, "shell":
    False}``); silent + idempotent on platforms with no
    handlers configured.
    """
    cfg = config if config is not None else load_config()
    if not cfg.enabled:
        return {"desktop": False, "shell": False}
    status = (
        record.status if hasattr(record, "status")
        else record.get("status", "")
    )
    if cfg.on_status and status not in cfg.on_status:
        return {"desktop": False, "shell": False}
    plan_name = (
        record.plan_name if hasattr(record, "plan_name")
        else record.get("plan_name", "")
    )
    title = f"arq-backup: {plan_name or 'run'}"
    body_bits = [f"status={status}"]
    error = (
        record.error if hasattr(record, "error")
        else record.get("error")
    )
    if error:
        body_bits.append(f"error: {error[:120]}")
    body = " — ".join(body_bits)
    out = {"desktop": False, "shell": False}
    if cfg.desktop_kind == "osascript":
        out["desktop"] = _send_macos(title, body)
    elif cfg.desktop_kind == "notify-send":
        out["desktop"] = _send_notify_send(title, body)
    if cfg.shell_command:
        env = {
            "STATUS": status,
            "PLAN_NAME": plan_name or "",
            "ERROR": error or "",
        }
        # JSON for stdin gives the operator's hook all fields
        # at once (richer than the env vars).
        try:
            from dataclasses import asdict
            payload = json.dumps(
                asdict(record), ensure_ascii=False, default=str,
            )
        except TypeError:
            payload = json.dumps({
                "status": status, "plan_name": plan_name,
                "error": error,
            })
        out["shell"] = _send_shell(
            cfg.shell_command, env, payload,
        )
    return out
