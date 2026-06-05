"""A small yes/no confirmation popup.

Returns ``True`` (confirmed) / ``False`` (cancelled) via
``ModalScreen.dismiss``. Compact + translucent like the other modals so
the screen behind stays visible. ``Enter`` confirms, ``Esc`` cancels.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ConfirmModal(ModalScreen[bool]):
    """Ask the operator to confirm a (potentially destructive) action."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
        background: $background 55%;
    }
    ConfirmModal > Vertical {
        width: 64;
        max-width: 90%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    ConfirmModal Label.title {
        text-style: bold;
        margin-bottom: 1;
    }
    ConfirmModal #buttons {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    ConfirmModal Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        message: str,
        confirm_label: str = "Delete",
        confirm_variant: str = "error",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.confirm_label = confirm_label
        self.confirm_variant = confirm_variant

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.title_text, classes="title")
            yield Static(self.message)
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    self.confirm_label, id="confirm",
                    variant=self.confirm_variant,
                )

    def on_mount(self) -> None:
        # Default focus on Cancel — a destructive confirm shouldn't be
        # one stray Enter away.
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)
