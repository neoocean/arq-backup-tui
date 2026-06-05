"""Home screen — the app's landing surface.

For M1 this is a working dashboard with three sections:

- **Plans**: lists known plans (empty on a fresh install). M3 will
  add a "New plan" path; for now we render a hint pointing at the
  not-yet-implemented action.
- **Quick actions**: Browse backup sets / Validate / Quit. The
  first two are hooked up to placeholder screens in later
  milestones; for M1 they just show a "Coming soon" notification.
- **Status bar** with key bindings.

Anything more (real plan launch, real browse / validate flows) is
deferred to its respective milestone.
"""

from __future__ import annotations

from typing import List

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    ListItem,
    ListView,
    Static,
    Tree,
)

from ..state import Plan


class HomeScreen(Screen):
    """The app's landing screen."""

    BINDINGS = [
        Binding("n", "new_plan", "Add Backup Plan", show=True),
        Binding("e", "edit_focused", "Edit Backup Plan", show=True),
        Binding("r", "run_focused", "Back Up Now", show=True),
        Binding("b", "browse", "Storage Locations", show=True),
        Binding("v", "validate", "Validate", show=True),
        Binding("a", "activity", "Activity Log", show=True),
        Binding("s", "scheduling", "Scheduling", show=True),
        Binding("q", "app.quit", "Quit", show=True),
        # Left arrow from the content area jumps focus back to the
        # sidebar menu (widgets that use Left themselves — the right-
        # pane Tree, text Inputs — consume it first, so this only fires
        # from the central lists/buttons).
        Binding("left", "focus_sidebar", "Menu", show=False),
    ]

    DEFAULT_CSS = """
    HomeScreen {
        layout: vertical;
    }
    HomeScreen #home-root {
        height: 1fr;
    }
    HomeScreen #home-content {
        width: 1fr;
        height: 1fr;
    }
    HomeScreen #panel-plans {
        height: 1fr;
    }
    HomeScreen #plans-list-col {
        width: 1fr;
    }
    HomeScreen #plan-detail-col {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
        margin: 1 1 0 0;
    }
    HomeScreen #plan-detail-col > .section-title {
        margin: 0 0 1 0;
    }
    HomeScreen #plans-section {
        height: auto;
        padding: 1 2;
        border: round $primary;
        margin: 1 1 0 1;
    }
    HomeScreen #actions-section {
        height: auto;
        padding: 1 2;
        border: round $primary;
        margin: 1 1 0 1;
    }
    HomeScreen .section-title {
        text-style: bold;
        margin-bottom: 1;
    }
    HomeScreen .empty-hint {
        color: $text-muted;
        text-style: italic;
    }
    /* Editable (own) plans bright; read-only Arq-mirrored plans dim,
       so editability is readable at a glance. */
    HomeScreen .plan-own {
        color: $text;
    }
    HomeScreen .plan-arq {
        color: $text-muted;
    }
    HomeScreen ListView {
        height: auto;
        max-height: 12;
    }
    HomeScreen .action-row {
        height: 3;
        align: left middle;
    }
    HomeScreen Button {
        margin: 0 1 0 0;
    }
    /* The default-variant buttons (Storage Locations / Validate) were
       near-invisible dark-on-dark. Lift them onto $panel with a clear
       outline so their edges read; the primary / error variants keep
       their own fills. */
    HomeScreen #action-browse, HomeScreen #action-validate {
        background: $panel;
        border: tall $surface-lighten-2;
    }
    HomeScreen #action-browse:hover, HomeScreen #action-validate:hover {
        background: $boost;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        plans = self._load_plans()
        # Persistent shell: a fixed Sidebar on the left drives a
        # ContentSwitcher on the right. Selecting a sidebar section
        # swaps the right-hand panel in place (no full-screen push) —
        # the macOS-Arq source-list model. The sections beyond Plans
        # are reusable panel widgets shared with their standalone
        # screen wrappers.
        from ..widgets.sidebar import Sidebar
        from .backup_sets import StoragePanel
        from .runs_monitor import ActivityPanel
        from .summary import SummaryPanel
        from .validate_run import ValidatePanel
        from .help import HelpPanel
        from textual.widgets import ContentSwitcher
        with Horizontal(id="home-root"):
            yield Sidebar(active="plans")
            with ContentSwitcher(initial="panel-plans", id="home-content"):
                with Horizontal(id="panel-plans"):
                    # Middle column: the plan list + quick actions.
                    with Vertical(id="plans-list-col"):
                        with Vertical(id="plans-section"):
                            yield Static(
                                "Backup Plans", classes="section-title",
                            )
                            if plans:
                                yield ListView(
                                    *[
                                        _PlanItem(p, self._render_plan_row(p))
                                        for p in plans
                                    ],
                                    id="plans-list",
                                )
                            else:
                                yield Static(
                                    "No backup plans yet — press 'n' to "
                                    "add one.",
                                    classes="empty-hint",
                                    id="plans-empty",
                                )
                        with Vertical(id="actions-section"):
                            yield Static(
                                "Quick actions", classes="section-title",
                            )
                            with Horizontal(classes="action-row"):
                                yield Button(
                                    "Add Backup Plan (n)", id="action-new",
                                    variant="primary",
                                )
                                yield Button(
                                    "Storage Locations (b)",
                                    id="action-browse",
                                )
                                yield Button(
                                    "Validate (v)", id="action-validate",
                                )
                                yield Button(
                                    "Quit (q)", id="action-quit",
                                    variant="error",
                                )
                    # Right column: the focused plan's details as a tree.
                    with Vertical(id="plan-detail-col"):
                        yield Static("Plan", classes="section-title")
                        yield Static(
                            "Select a plan to see its details.",
                            classes="empty-hint", id="plan-detail-hint",
                        )
                        detail: Tree[None] = Tree("", id="plan-detail-tree")
                        detail.show_root = True
                        detail.display = False
                        yield detail
                yield SummaryPanel(id="panel-summary")
                yield StoragePanel(id="panel-browse")
                yield ActivityPanel(
                    state_dir=self.app.runs_state_dir, id="panel-activity",
                )
                yield ValidatePanel(id="panel-validate")
                yield HelpPanel(id="panel-help")

        yield Footer()

    def _load_plans(self) -> List[Plan]:
        """Own plans (from our config dir) merged with the read-only
        mirror of a locally-installed Arq.app's plans.

        When both tools are present the two lists are unified into one
        and sorted by name, so the operator sees a single plan surface
        that matches what the Arq GUI shows. Arq-sourced rows carry
        ``origin == "arq"`` and are badged + non-editable. A plan we
        own that happens to share an id with an Arq plan (same
        planUUID) wins, so an operator can "adopt" an Arq plan by
        saving their own copy without it showing up twice."""
        own = self.app.plan_registry.list_plans()
        arq_src = getattr(self.app, "arq_app", None)
        if arq_src is None:
            return own
        own_ids = {p.plan_id for p in own if p.plan_id}
        # Mirror only *active* Arq plans — server.db keeps deactivated /
        # deleted plans around (inactive rows), but the Arq GUI shows
        # only active ones, so match that.
        mirrored = [
            ap.to_plan() for ap in arq_src.plans(active_only=True)
        ]
        merged = own + [
            p for p in mirrored if p.plan_id not in own_ids
        ]
        merged.sort(key=lambda pl: pl.name.lower())
        return merged

    @staticmethod
    def _render_plan_row(p: Plan) -> str:
        suffix = (
            f"   Last backup: {p.last_run_iso}" if p.last_run_iso
            else "   Not backed up yet"
        )
        sources_summary = (
            f"{len(p.sources)} source{'s' if len(p.sources) != 1 else ''}"
        )
        # Badge Arq-mirrored plans so the unified list makes the origin
        # obvious at a glance (matches the user's "unified list + origin
        # badge" choice).
        badge = "◆ Arq  " if p.origin == "arq" else ""
        return f"{badge}{p.name} ({sources_summary}){suffix}"

    # ------------------------------------------------------------------
    # Plan detail (right column) — updates as the cursor moves the list
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        # Populate the detail column for the first plan so the right
        # pane isn't blank before the operator moves the cursor.
        try:
            lv = self.query_one("#plans-list", ListView)
        except Exception:
            return
        for child in lv.children:
            if isinstance(child, _PlanItem):
                self._render_plan_detail(child.plan)
                break

    def on_list_view_highlighted(self, event) -> None:
        # Master-detail: highlighting a plan in the middle list renders
        # its info in the right column. Building the tree is cheap (no
        # IO), so doing it on every cursor move stays responsive. Lists
        # in other panels carry non-_PlanItem rows, so they're ignored.
        item = getattr(event, "item", None)
        if isinstance(item, _PlanItem):
            self._render_plan_detail(item.plan)

    def _render_plan_detail(self, plan: Plan) -> None:
        try:
            tree = self.query_one("#plan-detail-tree", Tree)
            hint = self.query_one("#plan-detail-hint", Static)
        except Exception:
            return
        hint.display = False
        tree.display = True
        tree.clear()
        root = tree.root
        root.label = plan.name or "(unnamed plan)"
        root.add_leaf(
            "Origin: "
            + ("Arq.app (read-only)" if plan.origin == "arq" else "this tool")
        )
        d = plan.destination or {}
        if plan.destination_kind == "sftp":
            dest_str = (
                f"sftp://{d.get('user', '?')}@{d.get('host', '?')}"
                f":{d.get('port', 22)}{d.get('path', '')}"
            )
        else:
            dest_str = d.get("path") or "(cloud / not openable)"
        root.add_leaf(f"Destination: {dest_str}")
        root.add_leaf(f"Chunker: {plan.chunker}")
        root.add_leaf(
            f"Last backup: {plan.last_run_iso or 'Not backed up yet'}"
        )
        src_node = root.add(f"Sources ({len(plan.sources)})")
        for s in plan.sources:
            src_node.add_leaf(s)
        if plan.retention:
            ret = root.add("Retention")
            for k, v in plan.retention.items():
                ret.add_leaf(f"{k}: {v}")
        if plan.exclude_globs or plan.exclude_regexes:
            ex = root.add("Excludes")
            for g in plan.exclude_globs:
                ex.add_leaf(f"glob: {g}")
            for r in plan.exclude_regexes:
                ex.add_leaf(f"regex: {r}")
        if plan.use_apfs_snapshot:
            root.add_leaf("APFS snapshot: yes")
        root.expand_all()

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def action_new_plan(self) -> None:
        from .plan_wizard import PlanWizardScreen
        self.app.push_screen(PlanWizardScreen(), self._after_wizard)

    def _after_wizard(self, _result) -> None:
        # The wizard saves itself + pops; we just need to refresh
        # the plans list so the new entry shows up.
        self._refresh_plans()

    def _show_section(self, section: str) -> None:
        """Swap the right-hand content panel to ``section`` and keep
        the sidebar highlight in lockstep. The single place section
        navigation flows through — both the sidebar (mouse / arrows +
        Enter) and the [b]/[v]/[a] key shortcuts call it, so they never
        push a full screen."""
        from textual.widgets import ContentSwitcher
        try:
            self.query_one("#home-content", ContentSwitcher).current = (
                f"panel-{section}"
            )
        except Exception:
            return
        from ..widgets.sidebar import Sidebar
        try:
            self.query_one(Sidebar).set_active(section)
        except Exception:
            pass
        # Move focus into the freshly-shown panel so the keyboard acts
        # on it immediately — its arrow keys + bindings take over,
        # rather than leaving focus stranded on the sidebar / old panel.
        try:
            panel = self.query_one(f"#panel-{section}")
            for w in panel.query("*"):
                if getattr(w, "can_focus", False) and w.display:
                    w.focus()
                    break
        except Exception:
            pass

    def action_focus_sidebar(self) -> None:
        """Move focus to the sidebar menu (Left-arrow from content)."""
        from ..widgets.sidebar import Sidebar
        try:
            self.query_one(Sidebar).focus()
        except Exception:
            pass

    def action_browse(self) -> None:
        self._show_section("browse")

    def action_validate(self) -> None:
        self._show_section("validate")

    def action_activity(self) -> None:
        """Show the activity log panel — every backup / restore /
        validate run on this host (incl. cron-driven ones the TUI
        didn't start), plus the mirrored Arq.app activity log."""
        self._show_section("activity")

    def on_sidebar_navigation(self, event) -> None:
        """Sidebar selection (click / arrow+Enter) → swap the right
        content panel. ``plans`` / ``activity`` / ``browse`` /
        ``validate`` / ``help`` each map to a ``panel-<section>`` in
        the ContentSwitcher."""
        section = getattr(event, "section", "")
        self._show_section(section)

    def action_scheduling(self) -> None:
        """Open the scheduling screen — list / install / remove
        cron + launchd schedules for plans.

        When a plan is focused, that plan rides along as the
        ``staged_plan`` so the operator can immediately press [i]
        / [l] to install a schedule for it. When no plan is
        focused, the screen opens in pure-list mode (no plans →
        no install — that's fine + expected)."""
        from .scheduling import SchedulingScreen
        # Quiet variant of _focused_plan — don't notify when no
        # plans exist (the scheduling screen is still useful for
        # listing existing schedules even with zero plans).
        plans = self._load_plans()
        focused = None
        if plans:
            try:
                lv = self.query_one("#plans-list")
                idx = (
                    int(lv.index)
                    if getattr(lv, "index", None) is not None
                    else 0
                )
                focused = plans[
                    max(0, min(idx, len(plans) - 1))
                ]
            except Exception:
                focused = plans[0]
        self.app.push_screen(
            SchedulingScreen(staged_plan=focused),
        )

    def action_run_focused(self) -> None:
        plan = self._focused_plan()
        if plan is None:
            return
        self._run_plan(plan)

    def action_edit_focused(self) -> None:
        plan = self._focused_plan()
        if plan is None:
            return
        if plan.origin == "arq":
            # Arq-mirrored plans are read-only — editing one would
            # silently fork a divergent copy into our config dir while
            # Arq keeps managing the original. Steer the operator to
            # Arq.app for edits (or to [n] for an independent plan).
            self.notify(
                f"'{plan.name}' is managed by Arq.app (read-only here). "
                "Edit it in Arq, or press 'n' to create an independent "
                "plan.",
                severity="warning",
            )
            return
        from .plan_wizard import PlanWizardScreen
        self.app.push_screen(
            PlanWizardScreen(plan=plan), self._after_wizard,
        )

    def _focused_plan(self):
        """Resolve the currently-focused plan, or surface a hint
        if there are none. Used by both [r] and [e]."""
        plans = self._load_plans()
        if not plans:
            self.notify(
                "No backup plans yet — press 'n' to add one.",
                severity="warning",
            )
            return None
        idx = 0
        try:
            list_view = self.query_one("#plans-list")
            if getattr(list_view, "index", None) is not None:
                idx = int(list_view.index)
        except Exception:
            pass
        return plans[max(0, min(idx, len(plans) - 1))]

    def _refresh_plans(self) -> None:
        # Refresh by recomposing the plans-section from scratch:
        # easier than mutating ListView in place when the empty
        # state may need to flip on/off.
        self.app.pop_screen()
        self.app.push_screen(HomeScreen())

    def _run_plan(self, plan) -> None:
        # Resolve / prompt for the encryption password, then push
        # the BackupRunScreen.
        from ..state import Destination
        from ..widgets.password_modal import PasswordModal
        from .backup_run import BackupRunScreen

        # An Arq plan that targets a cloud backend (S3 / B2 / Glacier /
        # Arq Premium / …) has no destination we can open — those are
        # deliberately out of scope (README §1). Surface that instead
        # of launching a backup at an empty path.
        d0 = plan.destination or {}
        if not (d0.get("path") or d0.get("host")):
            self.notify(
                f"'{plan.name}' targets a cloud destination this tool "
                "doesn't open (local / NAS / SFTP only). Run it from "
                "Arq.app.",
                severity="warning",
            )
            return

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
        cached_pw = self.app.credential_cache.get_encryption_password(dest)
        if cached_pw is not None:
            self.app.push_screen(BackupRunScreen(plan=plan, password=cached_pw))
            return

        def _with_pw(pw):
            if not pw:
                return
            self.app.credential_cache.set_encryption_password(dest, pw)
            self.app.push_screen(BackupRunScreen(plan=plan, password=pw))
        self.app.push_screen(
            PasswordModal(
                prompt=f"Encryption password for {plan.name}",
            ),
            _with_pw,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "action-new":
            self.action_new_plan()
        elif event.button.id == "action-browse":
            self.action_browse()
        elif event.button.id == "action-validate":
            self.action_validate()
        elif event.button.id == "action-quit":
            self.app.exit()


class _PlanItem(ListItem):
    """A plan row in the middle list. Holds the :class:`Plan` so the
    right-hand detail column can render it on highlight. Editable own
    plans render bright; read-only Arq-mirrored plans render dim."""

    def __init__(self, plan: Plan, label: str) -> None:
        cls = "plan-arq" if plan.origin == "arq" else "plan-own"
        super().__init__(Static(label, classes=cls))
        self.plan = plan
