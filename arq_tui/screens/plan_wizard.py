"""Plan creation wizard.

Single-screen, step-based flow:

1. Sources    — multi-source picker
2. Destination— local path or SFTP coordinates
3. Encryption — password (M3 doesn't fall back to prompts mid-run;
                the password is captured here and cached)
4. Chunker    — generic / Arq.app v7.41 / no chunking
5. Review     — name + summary, save

Each step is its own ``Vertical`` swapped in/out. ``Next`` /
``Back`` navigation only — the wizard validates per step before
advancing.

Plan editing is deliberately deferred to a later milestone (per
the project decision in chat); this screen only creates new plans.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import List

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
)

from ..state import Plan
from ..widgets.source_picker import SourcePicker


@dataclass
class _Draft:
    """In-progress wizard state."""

    sources: List[str] = field(default_factory=list)
    destination_kind: str = "local"
    destination: dict = field(default_factory=dict)
    encryption_password: str = ""
    chunker: str = "default"
    use_packs: bool = True
    dedup_against_existing: bool = True
    name: str = ""


class PlanWizardScreen(Screen):
    """Multi-step wizard for creating a backup plan."""

    STEPS = ("sources", "destination", "encryption", "chunker", "review")

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back to Home", show=True),
    ]

    DEFAULT_CSS = """
    PlanWizardScreen #container {
        padding: 1 2;
        height: 1fr;
    }
    PlanWizardScreen .step-title {
        text-style: bold;
        margin-bottom: 1;
    }
    PlanWizardScreen .field-label {
        color: $text-muted;
        margin-top: 1;
    }
    PlanWizardScreen .step-pane {
        display: none;
        height: 1fr;
    }
    PlanWizardScreen .step-pane.-active {
        display: block;
    }
    PlanWizardScreen #nav-row {
        height: 3;
        align: right middle;
    }
    PlanWizardScreen #nav-row Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.draft = _Draft()
        self._step_index = 0

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="container"):
            yield Static("New plan", classes="step-title")
            yield Static(
                self._step_indicator(),
                id="step-indicator",
            )

            # Step 1 — sources
            with Vertical(
                id="pane-sources", classes="step-pane -active",
            ):
                yield Label("Source folders", classes="step-title")
                yield SourcePicker()

            # Step 2 — destination
            with Vertical(id="pane-destination", classes="step-pane"):
                yield Label("Destination", classes="step-title")
                yield Label("Kind", classes="field-label")
                with RadioSet(id="dest-kind"):
                    yield RadioButton(
                        "Local filesystem", value=True, id="dest-kind-local",
                    )
                    yield RadioButton("SFTP", id="dest-kind-sftp")
                yield Label(
                    "Local path (kind=Local)", classes="field-label",
                )
                yield Input(
                    placeholder="/Volumes/arqbackup1", id="dest-local-path",
                )
                yield Label("SFTP host", classes="field-label")
                yield Input(placeholder="example.com", id="dest-sftp-host")
                yield Label("SFTP user", classes="field-label")
                yield Input(placeholder="u123", id="dest-sftp-user")
                yield Label("SFTP port", classes="field-label")
                yield Input(value="22", id="dest-sftp-port")
                yield Label("Remote root", classes="field-label")
                yield Input(
                    placeholder="/home/u123/arq", id="dest-sftp-root",
                )
                yield Label(
                    "SFTP identity file (blank = password auth)",
                    classes="field-label",
                )
                yield Input(id="dest-sftp-identity")

            # Step 3 — encryption
            with Vertical(id="pane-encryption", classes="step-pane"):
                yield Label("Encryption password", classes="step-title")
                yield Input(
                    password=True, id="enc-password",
                    placeholder="password",
                )
                yield Label(
                    "Confirm password", classes="field-label",
                )
                yield Input(password=True, id="enc-password2")
                yield Label(
                    "Existing destinations: must match the prior keyset.",
                    classes="field-label",
                )

            # Step 4 — chunker
            with Vertical(id="pane-chunker", classes="step-pane"):
                yield Label("Chunker", classes="step-title")
                with RadioSet(id="chunker-set"):
                    yield RadioButton(
                        "Generic Buzhash (default)",
                        value=True, id="chunker-default",
                    )
                    yield RadioButton(
                        "Match Arq.app v7.41 parameters",
                        id="chunker-arq",
                    )
                    yield RadioButton(
                        "No chunking (one blob per file)",
                        id="chunker-none",
                    )
                yield Label("Storage layout", classes="field-label")
                with RadioSet(id="layout-set"):
                    yield RadioButton(
                        "Packed (treepacks/ + blobpacks/)",
                        value=True, id="layout-packs",
                    )
                    yield RadioButton(
                        "Standalone objects",
                        id="layout-standalone",
                    )
                yield Label(
                    "Cross-run dedup against existing destination",
                    classes="field-label",
                )
                with RadioSet(id="dedup-set"):
                    yield RadioButton(
                        "On (recommended)", value=True, id="dedup-on",
                    )
                    yield RadioButton("Off", id="dedup-off")

            # Step 5 — review
            with Vertical(id="pane-review", classes="step-pane"):
                yield Label("Plan name + review", classes="step-title")
                yield Label("Plan name", classes="field-label")
                yield Input(
                    placeholder="home-laptop-to-nas", id="plan-name",
                )
                yield Label("Summary", classes="field-label")
                yield Static("(filled when reaching this step)", id="review")

            with Horizontal(id="nav-row"):
                yield Button("Back", id="nav-back")
                yield Button("Next", id="nav-next", variant="primary")
        yield Footer()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _step_indicator(self) -> str:
        idx = self._step_index
        return f"Step {idx + 1} of {len(self.STEPS)}: {self.STEPS[idx]}"

    def _show_step(self, index: int) -> None:
        self._step_index = max(0, min(index, len(self.STEPS) - 1))
        for i, name in enumerate(self.STEPS):
            pane = self.query_one(f"#pane-{name}", Vertical)
            if i == self._step_index:
                pane.add_class("-active")
            else:
                pane.remove_class("-active")
        self.query_one("#step-indicator", Static).update(
            self._step_indicator()
        )
        if self.STEPS[self._step_index] == "review":
            self._fill_review()
        # Toggle the Next button label between "Next" / "Save".
        nav_next = self.query_one("#nav-next", Button)
        nav_next.label = (
            "Save" if self._step_index == len(self.STEPS) - 1 else "Next"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nav-back":
            self._show_step(self._step_index - 1)
        elif event.button.id == "nav-next":
            self._handle_next()

    def _handle_next(self) -> None:
        step = self.STEPS[self._step_index]
        ok = self._capture_step(step)
        if not ok:
            return
        if self._step_index == len(self.STEPS) - 1:
            self._save_and_exit()
        else:
            self._show_step(self._step_index + 1)

    # ------------------------------------------------------------------
    # Per-step capture / validation
    # ------------------------------------------------------------------

    def _capture_step(self, step: str) -> bool:
        if step == "sources":
            picker = self.query_one(SourcePicker)
            if not picker.paths:
                self.notify(
                    "Add at least one source folder.",
                    severity="error",
                )
                return False
            self.draft.sources = list(picker.paths)
            return True
        if step == "destination":
            kind = (
                "sftp" if self.query_one(
                    "#dest-kind-sftp", RadioButton,
                ).value else "local"
            )
            if kind == "local":
                path = self.query_one(
                    "#dest-local-path", Input,
                ).value.strip()
                if not path:
                    self.notify(
                        "Local path is required.", severity="error",
                    )
                    return False
                self.draft.destination_kind = "local"
                self.draft.destination = {"path": path}
                return True
            host = self.query_one(
                "#dest-sftp-host", Input,
            ).value.strip()
            user = self.query_one(
                "#dest-sftp-user", Input,
            ).value.strip()
            port_text = self.query_one(
                "#dest-sftp-port", Input,
            ).value.strip() or "22"
            root = self.query_one(
                "#dest-sftp-root", Input,
            ).value.strip()
            identity = self.query_one(
                "#dest-sftp-identity", Input,
            ).value.strip()
            if not host or not user or not root:
                self.notify(
                    "SFTP host, user, and root are required.",
                    severity="error",
                )
                return False
            try:
                port = int(port_text)
            except ValueError:
                self.notify(
                    "Port must be a number.", severity="error",
                )
                return False
            self.draft.destination_kind = "sftp"
            self.draft.destination = {
                "host": host, "user": user, "port": port,
                "path": root, "identity_file": identity,
            }
            return True
        if step == "encryption":
            pw1 = self.query_one("#enc-password", Input).value
            pw2 = self.query_one("#enc-password2", Input).value
            if not pw1:
                self.notify(
                    "Password cannot be empty.", severity="error",
                )
                return False
            if pw1 != pw2:
                self.notify(
                    "Passwords don't match.", severity="error",
                )
                return False
            self.draft.encryption_password = pw1
            return True
        if step == "chunker":
            if self.query_one("#chunker-arq", RadioButton).value:
                self.draft.chunker = "arq_v7_41"
            elif self.query_one("#chunker-none", RadioButton).value:
                self.draft.chunker = "none"
            else:
                self.draft.chunker = "default"
            self.draft.use_packs = self.query_one(
                "#layout-packs", RadioButton,
            ).value
            self.draft.dedup_against_existing = self.query_one(
                "#dedup-on", RadioButton,
            ).value
            return True
        if step == "review":
            name = self.query_one("#plan-name", Input).value.strip()
            if not name:
                self.notify(
                    "Plan name is required.", severity="error",
                )
                return False
            self.draft.name = name
            return True
        return True

    # ------------------------------------------------------------------
    # Review pane fill
    # ------------------------------------------------------------------

    def _fill_review(self) -> None:
        d = self.draft
        lines = [
            f"Sources ({len(d.sources)}):",
        ]
        for s in d.sources:
            lines.append(f"  • {s}")
        lines.append("Destination:")
        if d.destination_kind == "local":
            lines.append(f"  local: {d.destination.get('path', '')}")
        else:
            dst = d.destination
            lines.append(
                f"  sftp: {dst.get('user', '?')}@"
                f"{dst.get('host', '?')}:"
                f"{dst.get('port', 22)}"
                f"{dst.get('path', '')}"
            )
        lines.append(f"Chunker: {d.chunker}")
        lines.append(f"Layout: {'packs' if d.use_packs else 'standalone'}")
        lines.append(
            f"Dedup against existing: "
            f"{'on' if d.dedup_against_existing else 'off'}"
        )
        self.query_one("#review", Static).update("\n".join(lines))

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save_and_exit(self) -> None:
        d = self.draft
        plan = Plan(
            plan_id=str(uuid.uuid4()).upper(),
            name=d.name,
            sources=list(d.sources),
            destination_kind=d.destination_kind,
            destination=dict(d.destination),
            chunker=d.chunker,
            use_packs=d.use_packs,
            dedup_against_existing=d.dedup_against_existing,
        )
        try:
            self.app.plan_registry.save(plan)
        except Exception as exc:
            self.notify(
                f"Could not save plan: {exc}", severity="error",
            )
            return
        # Cache the encryption password for this plan's destination
        # so the run screen doesn't immediately re-prompt.
        from ..state import Destination
        dest = self._draft_to_destination(plan)
        self.app.credential_cache.set_encryption_password(
            dest, d.encryption_password,
        )
        self.app.destination_store.add_or_touch(dest)
        self.notify(f"Plan '{plan.name}' saved.", severity="information")
        self.app.pop_screen()

    @staticmethod
    def _draft_to_destination(plan: Plan):
        from ..state import Destination
        if plan.destination_kind == "local":
            return Destination(
                kind="local",
                label=plan.name,
                path=plan.destination.get("path", ""),
            )
        return Destination(
            kind="sftp",
            label=plan.name,
            host=plan.destination.get("host", ""),
            port=int(plan.destination.get("port") or 22),
            user=plan.destination.get("user", ""),
            path=plan.destination.get("path", ""),
            identity_file=plan.destination.get("identity_file", ""),
        )
