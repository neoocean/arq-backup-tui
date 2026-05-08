"""Command dispatch for :class:`~arq_tui.widgets.console.CommandConsole`.

Maps slash-command lines (``:new-plan``, ``:run home-laptop``, …)
to existing app actions / screen pushes. Every command is
defined once in :data:`_COMMANDS`, surfaces in ``:help`` for
free, and returns a string that the console writes back to its
log so the operator gets immediate feedback.

Commands match keybindings the rest of the TUI already exposes —
the console is a typeable alias, not a parallel control plane.
``:browse`` does the same thing as ``[b]`` on the home screen,
``:new-plan`` matches ``[n]``, etc. Anything that needs argument
parsing (``:run <name>``, ``:edit <name>``, ``:delete <name>``)
resolves the name against the plan registry with case-insensitive
prefix matching, the same convention the headless ``arq-tui plans``
CLI uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, List, Optional

if TYPE_CHECKING:
    from .app import ArqTuiApp
    from .state import Plan


@dataclass
class _Command:
    """One slash-command entry."""

    name: str
    summary: str
    handler: "Callable[[ArqTuiApp, str], Awaitable[str]]"
    aliases: tuple = ()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _resolve_plan(app: "ArqTuiApp", q: str) -> "Optional[Plan]":
    """Same matcher as ``arq_tui.cli._resolve_plan`` — exact UUID,
    exact name, or unique prefix on either."""
    plans = app.plan_registry.list_plans()
    q_lower = q.lower()
    for p in plans:
        if p.plan_id.lower() == q_lower:
            return p
    for p in plans:
        if p.name == q:
            return p
    matches = [
        p for p in plans
        if p.plan_id.lower().startswith(q_lower)
        or p.name.lower().startswith(q_lower)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


async def _cmd_help(app: "ArqTuiApp", arg: str) -> str:
    lines = ["[bold]commands:[/]"]
    for cmd in _COMMANDS:
        all_names = ", ".join((cmd.name, *cmd.aliases))
        lines.append(f"  [b]:{all_names}[/] — {cmd.summary}")
    lines.append("")
    lines.append(
        "[dim]Esc closes the console; ↑/↓ walks history; "
        "leading [b]:[/] / [b]/[/] / [b]\\\\[/] are all optional.[/]"
    )
    return "\n".join(lines)


async def _cmd_quit(app: "ArqTuiApp", arg: str) -> str:
    app.exit()
    return "[dim]exiting…[/]"


async def _cmd_close(app: "ArqTuiApp", arg: str) -> str:
    if app.console_widget is not None:
        app.console_widget.close()
    return ""


async def _cmd_clear(app: "ArqTuiApp", arg: str) -> str:
    if app.console_widget is not None:
        from textual.widgets import RichLog
        app.console_widget.query_one(
            "#console-log", RichLog,
        ).clear()
    return ""


async def _cmd_toggle_theme(app: "ArqTuiApp", arg: str) -> str:
    app.action_toggle_theme()
    return f"theme = {getattr(app, 'theme', '?')}"


async def _cmd_help_screen(app: "ArqTuiApp", arg: str) -> str:
    if app.console_widget is not None:
        app.console_widget.close()
    app.action_help()
    return ""


async def _cmd_home(app: "ArqTuiApp", arg: str) -> str:
    # Pop screens until HomeScreen is on top. Conservative: cap
    # at 8 pops so a malformed stack can't infinite-loop.
    from .screens.home import HomeScreen
    if app.console_widget is not None:
        app.console_widget.close()
    for _ in range(8):
        if isinstance(app.screen, HomeScreen):
            break
        try:
            app.pop_screen()
        except Exception:
            break
    return ""


async def _cmd_browse(app: "ArqTuiApp", arg: str) -> str:
    from .screens.backup_sets import BackupSetListScreen
    if app.console_widget is not None:
        app.console_widget.close()
    app.push_screen(BackupSetListScreen())
    return ""


async def _cmd_validate(app: "ArqTuiApp", arg: str) -> str:
    # Validation runs against an opened destination, which is
    # owned by BackupSetListScreen. Mirror what HomeScreen's [v]
    # does — surface the screen and let the operator pick a
    # destination there.
    return await _cmd_browse(app, arg)


async def _cmd_new_plan(app: "ArqTuiApp", arg: str) -> str:
    from .screens.plan_wizard import PlanWizardScreen
    if app.console_widget is not None:
        app.console_widget.close()
    app.push_screen(PlanWizardScreen())
    return ""


async def _cmd_plans(app: "ArqTuiApp", arg: str) -> str:
    plans = app.plan_registry.list_plans()
    if not plans:
        return "[dim](no plans)[/]"
    rows = []
    for p in plans:
        rows.append(
            f"  [b]{p.name}[/] [dim]({p.plan_id[:8]}…)[/] "
            f"sources={len(p.sources)} dest={p.destination_kind} "
            f"last_run={p.last_run_iso or 'never'}"
        )
    return "\n".join(rows)


async def _cmd_edit(app: "ArqTuiApp", arg: str) -> str:
    if not arg:
        return "[red]usage: :edit <plan-name-or-id>[/]"
    plan = _resolve_plan(app, arg)
    if plan is None:
        return f"[red]no plan matches {arg!r}[/]"
    from .screens.plan_wizard import PlanWizardScreen
    if app.console_widget is not None:
        app.console_widget.close()
    app.push_screen(PlanWizardScreen(plan=plan))
    return ""


async def _cmd_run(app: "ArqTuiApp", arg: str) -> str:
    if not arg:
        return "[red]usage: :run <plan-name-or-id>[/]"
    plan = _resolve_plan(app, arg)
    if plan is None:
        return f"[red]no plan matches {arg!r}[/]"
    # Same path HomeScreen [r] uses — resolve the destination,
    # consult the credential cache, push BackupRunScreen.
    from .screens.backup_run import BackupRunScreen
    from .state import Destination
    if plan.destination_kind == "local":
        dest = Destination(
            kind="local", label=plan.name,
            path=str(plan.destination.get("path") or ""),
        )
    else:
        d = plan.destination
        dest = Destination(
            kind="sftp", label=plan.name,
            host=str(d.get("host") or ""),
            port=int(d.get("port") or 22),
            user=str(d.get("user") or ""),
            path=str(d.get("path") or ""),
            identity_file=str(d.get("identity_file") or ""),
        )
    cached = app.credential_cache.get_encryption_password(dest)
    if cached is None:
        return (
            f"[yellow]no cached password for {plan.name!r}; "
            f"open Home and press [b]r[/] to be prompted.[/]"
        )
    if app.console_widget is not None:
        app.console_widget.close()
    app.push_screen(BackupRunScreen(plan=plan, password=cached))
    return ""


async def _cmd_delete(app: "ArqTuiApp", arg: str) -> str:
    if not arg:
        return "[red]usage: :delete <plan-name-or-id>[/]"
    plan = _resolve_plan(app, arg)
    if plan is None:
        return f"[red]no plan matches {arg!r}[/]"
    removed = app.plan_registry.delete(plan.plan_id)
    if removed:
        return (
            f"[green]deleted[/] {plan.name!r} "
            f"({plan.plan_id[:8]}…)"
        )
    return f"[red]no file on disk for {plan.plan_id}[/]"


async def _cmd_back(app: "ArqTuiApp", arg: str) -> str:
    if app.console_widget is not None:
        app.console_widget.close()
    try:
        app.pop_screen()
    except Exception as exc:  # noqa: BLE001
        return f"[red]could not pop screen: {exc}[/]"
    return ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_COMMANDS: List[_Command] = [
    _Command("help", "show this list", _cmd_help, aliases=("?",)),
    _Command(
        "open-help", "open the full help screen",
        _cmd_help_screen, aliases=("help-screen",),
    ),
    _Command("home", "return to the Home screen", _cmd_home),
    _Command(
        "back", "pop the current screen",
        _cmd_back, aliases=("pop",),
    ),
    _Command(
        "browse", "open the backup-set browser",
        _cmd_browse, aliases=("b",),
    ),
    _Command(
        "validate", "validate an opened destination",
        _cmd_validate,
    ),
    _Command(
        "new-plan", "open the plan wizard",
        _cmd_new_plan, aliases=("new", "n"),
    ),
    _Command(
        "plans", "list saved plans inline", _cmd_plans,
    ),
    _Command(
        "edit", "edit a plan by name or id prefix",
        _cmd_edit, aliases=("e",),
    ),
    _Command(
        "run", "run a plan by name or id prefix",
        _cmd_run, aliases=("r",),
    ),
    _Command(
        "delete", "remove a plan file by name or id prefix",
        _cmd_delete, aliases=("rm",),
    ),
    _Command(
        "toggle-theme", "flip dark / light",
        _cmd_toggle_theme, aliases=("theme", "t"),
    ),
    _Command("clear", "clear the console log", _cmd_clear),
    _Command(
        "close", "close the console",
        _cmd_close, aliases=("q",),
    ),
    _Command(
        "quit", "exit the app",
        _cmd_quit, aliases=("exit",),
    ),
]


_COMMAND_BY_NAME = {}
for _c in _COMMANDS:
    _COMMAND_BY_NAME[_c.name] = _c
    for _alias in _c.aliases:
        _COMMAND_BY_NAME[_alias] = _c


async def dispatch_command(app: "ArqTuiApp", line: str) -> str:
    """Run one command line through the registry.

    The :class:`~arq_tui.widgets.console.CommandConsole` strips the
    leading sigil before calling — so this function only deals
    with bare ``cmd [args...]`` strings. Returns whatever rich
    text the handler wants written to the console log; an empty
    string suppresses the trailing log entry.
    """
    line = line.strip()
    if not line:
        return ""
    parts = line.split(maxsplit=1)
    cmd_name = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    cmd = _COMMAND_BY_NAME.get(cmd_name)
    if cmd is None:
        return (
            f"[red]unknown command:[/] {cmd_name!r} "
            f"(try [b]:help[/])"
        )
    return await cmd.handler(app, arg)
