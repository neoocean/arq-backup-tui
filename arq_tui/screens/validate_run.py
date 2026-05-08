"""Validation runner — wraps ``arq_validator.validate`` plus the
resumable audit-drip flow.

Launched from :class:`BackupSetListScreen` (which already has the
backend opened + password cached). Two halves:

- :class:`ValidateLaunchScreen` — picks a tier and any
  tier-specific options (audit-drip throttle, state file).
- :class:`ValidateRunScreen` — drives the worker and renders live
  progress.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

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

from ..widgets.progress_panel import ProgressPanel
from ..workers import (
    ValidateWorker,
    WorkerEvent,
    WorkerFailed,
    WorkerFinished,
)


TIER_VALUES = ("dry-run", "quick", "deep", "audit", "audit-drip")


class ValidateLaunchScreen(Screen):
    """Picks a tier + options, then pushes :class:`ValidateRunScreen`."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
    ]

    DEFAULT_CSS = """
    ValidateLaunchScreen {
        layout: vertical;
    }
    ValidateLaunchScreen #container {
        padding: 1 2;
    }
    ValidateLaunchScreen .step-title {
        text-style: bold;
        margin-bottom: 1;
    }
    ValidateLaunchScreen .field-label {
        color: $text-muted;
        margin-top: 1;
    }
    ValidateLaunchScreen #buttons {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    ValidateLaunchScreen Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(
        self,
        *,
        backend: Any,
        password: Optional[str],
        dest_label: str,
        config_dir: Path,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.password = password
        self.dest_label = dest_label
        self.config_dir = Path(config_dir)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="container"):
            yield Static(
                f"Validate: {self.dest_label}",
                classes="step-title",
            )
            yield Label("Tier", classes="field-label")
            with RadioSet(id="tier-set"):
                yield RadioButton("L0 — layout shape", id="tier-dry-run")
                yield RadioButton(
                    "L1a — ARQO magic-byte sweep",
                    id="tier-quick",
                )
                yield RadioButton(
                    "L1b — keyset + latest backuprecord HMAC",
                    value=True, id="tier-deep",
                )
                yield RadioButton(
                    "L2 — full HMAC sweep",
                    id="tier-audit",
                )
                yield RadioButton(
                    "Audit-drip (resumable L2)",
                    id="tier-drip",
                )

            yield Label(
                "Audit-drip max runtime (sec, 0 = run to completion)",
                classes="field-label",
            )
            yield Input(value="0", id="drip-runtime")
            yield Label(
                "Audit-drip rate (files/min, blank = uncapped)",
                classes="field-label",
            )
            yield Input(id="drip-rate")
            yield Label(
                "Audit-drip target name (used as state-file key)",
                classes="field-label",
            )
            yield Input(value="default", id="drip-target")

            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    "Start", id="start", variant="primary",
                )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.app.pop_screen()
            return
        if event.button.id == "start":
            self._launch()

    def _launch(self) -> None:
        tier = self._selected_tier()
        if tier in ("deep", "audit", "audit-drip") and not self.password:
            self.notify(
                "L1b / L2 / audit-drip need an encryption password.",
                severity="error",
            )
            return
        if tier == "audit-drip":
            target = self.query_one(
                "#drip-target", Input,
            ).value.strip() or "default"
            try:
                runtime = int(
                    self.query_one("#drip-runtime", Input).value.strip()
                    or "0"
                )
            except ValueError:
                runtime = 0
            rate_text = self.query_one(
                "#drip-rate", Input,
            ).value.strip()
            try:
                rate = float(rate_text) if rate_text else None
            except ValueError:
                rate = None
            state_file = (
                self.config_dir / "audit_drip" / f"{target}.json"
            )
            self.app.push_screen(ValidateRunScreen(
                backend=self.backend,
                tier="audit-drip",
                password=self.password or "",
                drip_target=target,
                drip_state_file=state_file,
                drip_max_runtime_sec=runtime,
                drip_rate_files_per_min=rate,
                dest_label=self.dest_label,
            ))
            return
        self.app.push_screen(ValidateRunScreen(
            backend=self.backend,
            tier=tier,
            password=self.password or "",
            dest_label=self.dest_label,
        ))

    def _selected_tier(self) -> str:
        for tier_value, button_id in zip(TIER_VALUES, (
            "tier-dry-run", "tier-quick", "tier-deep",
            "tier-audit", "tier-drip",
        )):
            if self.query_one(f"#{button_id}", RadioButton).value:
                return tier_value
        return "deep"


class ValidateRunScreen(Screen):
    """Drives one validation run + renders progress."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
    ]

    DEFAULT_CSS = """
    ValidateRunScreen #title {
        text-style: bold;
        padding: 1 2;
    }
    ValidateRunScreen #panel {
        border: round $primary;
        padding: 0 1;
        margin: 0 1;
    }
    ValidateRunScreen #summary {
        margin: 1 2;
    }
    """

    def __init__(
        self,
        *,
        backend: Any,
        tier: str,
        password: str,
        dest_label: str,
        drip_target: str = "",
        drip_state_file: Optional[Path] = None,
        drip_max_runtime_sec: int = 0,
        drip_rate_files_per_min: Optional[float] = None,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.tier = tier
        self.password = password
        self.dest_label = dest_label
        self.drip_target = drip_target
        self.drip_state_file = drip_state_file
        self.drip_max_runtime_sec = drip_max_runtime_sec
        self.drip_rate_files_per_min = drip_rate_files_per_min
        self.worker: Optional[Any] = None
        self._drip_worker: Optional[Any] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            f"Validating ({self.tier}): {self.dest_label}",
            id="title",
        )
        with Vertical(id="panel"):
            yield ProgressPanel()
        yield Static("", id="summary")
        yield Footer()

    def on_mount(self) -> None:
        if self.tier == "audit-drip":
            self._start_drip()
        else:
            self._start_validate()

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    def _start_validate(self) -> None:
        self.worker = ValidateWorker(
            self,
            backend=self.backend,
            tier=self.tier,
            password=self.password,
        )
        self.worker.start()

    def _start_drip(self) -> None:
        # The audit-drip API differs enough from validate() that
        # we can't share the worker. Build a one-off subclass
        # inline so the ProgressCb / cancel plumbing is consistent
        # with the rest of the bridge.
        from ..workers import _BaseWorker

        target = self
        backend = self.backend
        password = self.password
        drip_target = self.drip_target or "default"
        state_file = self.drip_state_file
        max_runtime = self.drip_max_runtime_sec
        rate = self.drip_rate_files_per_min

        class _DripWorker(_BaseWorker):
            def _run(self):
                from arq_validator.audit_drip import run_audit_drip
                state_file.parent.mkdir(parents=True, exist_ok=True)

                def adapt(event):
                    payload = dict(event.payload)
                    if event.message:
                        payload.setdefault("message", event.message)
                    self._emit(event.kind.value, payload)

                return run_audit_drip(
                    backend,
                    target=drip_target,
                    state_file=state_file,
                    encryption_password=password,
                    max_runtime_sec=max_runtime,
                    rate_files_per_min=rate,
                    callback=adapt,
                )

        self._drip_worker = _DripWorker(target)
        self._drip_worker.start()

    # ------------------------------------------------------------------
    # Worker messages
    # ------------------------------------------------------------------

    def on_worker_event(self, event: WorkerEvent) -> None:
        self.query_one(ProgressPanel).consume_event(
            event.kind, event.payload,
        )

    def on_worker_finished(self, event: WorkerFinished) -> None:
        panel = self.query_one(ProgressPanel)
        panel.finished = True
        summary = self.query_one("#summary", Static)
        summary.update(self._summarize(event.result))

    def on_worker_failed(self, event: WorkerFailed) -> None:
        panel = self.query_one(ProgressPanel)
        panel.failed = True
        panel.error_message = event.error
        panel.append_log(f"FAILED: {event.error}")

    def _summarize(self, result: Any) -> str:
        if result is None:
            return "(no result)"
        # validate() returns ValidationReport; audit_drip returns
        # AuditDripState. Render whichever we got.
        cls = result.__class__.__name__
        if cls == "ValidationReport":
            l0 = getattr(result, "l0", None)
            l1a = getattr(result, "l1a", None)
            l1b = getattr(result, "l1b", None)
            l2 = getattr(result, "l2", None)
            lines = [f"Tier: {result.tier}"]
            if l0 is not None:
                lines.append(f"  L0: ok={l0.layout_ok}")
            if l1a is not None:
                lines.append(
                    f"  L1a: {l1a.ok}/{l1a.total} ARQOs OK"
                )
            if l1b is not None:
                lines.append(
                    f"  L1b: {l1b.ok}/{l1b.total} backuprecord HMACs OK"
                )
            if l2 is not None:
                lines.append(
                    f"  L2: {l2.files_ok}/{l2.files_total} files OK "
                    f"({l2.bytes_ok} bytes)"
                )
            if getattr(result, "error", None):
                lines.append(f"  ERROR: {result.error}")
            return "\n".join(lines)
        if cls == "AuditDripState":
            return (
                f"Audit-drip target={result.target} "
                f"cursor={getattr(result, 'cursor', '?')} "
                f"checked={getattr(result, 'files_checked', '?')} "
                f"failed={len(getattr(result, 'failed', []) or [])}"
            )
        return repr(result)
