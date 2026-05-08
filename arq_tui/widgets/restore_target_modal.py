"""Prompt for the restore destination directory.

Dismisses with the chosen path string (or ``None`` on cancel).
The path is created if it doesn't already exist; the modal
doesn't try to validate the underlying filesystem beyond a
non-empty input check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class RestoreTargetModal(ModalScreen[Optional[str]]):
    """Ask the user where to write restored files."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=True)]

    DEFAULT_CSS = """
    RestoreTargetModal {
        align: center middle;
    }
    RestoreTargetModal > Vertical {
        width: 70;
        max-width: 95%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    RestoreTargetModal Label.title {
        text-style: bold;
        margin-bottom: 1;
    }
    RestoreTargetModal Label.hint {
        color: $text-muted;
    }
    RestoreTargetModal #buttons {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    RestoreTargetModal Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(self, *, summary: str = "") -> None:
        super().__init__()
        self.summary = summary

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Restore destination", classes="title")
            if self.summary:
                yield Label(self.summary, classes="hint")
            yield Input(
                placeholder="/path/to/restore/here",
                id="target-input",
            )
            yield Label(
                "Path will be created if it doesn't exist.",
                classes="hint",
            )
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Restore", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#target-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "submit":
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "target-input":
            self._submit()

    def _submit(self) -> None:
        raw = self.query_one("#target-input", Input).value.strip()
        if not raw:
            self.notify("Path is required.", severity="error")
            return
        path = str(Path(raw).expanduser())
        self.dismiss(path)

    def action_cancel(self) -> None:
        self.dismiss(None)
