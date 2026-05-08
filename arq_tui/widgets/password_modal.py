"""Modal password prompt.

Used wherever a screen needs the user's encryption password for a
destination — typically the first time the destination's keyset
needs decrypting in a session. Submits via ``Enter``, cancels via
``Escape``.

Passwords never touch the filesystem: this widget keeps the entered
text in memory only, hands it to the caller via a Textual message,
and releases its own reference when the modal dismisses.
"""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label


class PasswordModal(ModalScreen[Optional[str]]):
    """Prompt for an encryption password.

    Returns the entered password via ``ModalScreen.dismiss``; the
    caller awaits ``await app.push_screen_wait(PasswordModal(...))``
    or hooks the screen's result message.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    PasswordModal {
        align: center middle;
    }
    PasswordModal > Vertical {
        width: 60;
        max-width: 90%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    PasswordModal Label.title {
        text-style: bold;
        margin-bottom: 1;
    }
    PasswordModal Label.hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        prompt: str = "Encryption password",
        hint: str = "",
    ) -> None:
        super().__init__()
        self.prompt = prompt
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt, classes="title")
            yield Input(
                password=True, id="password-input",
                placeholder="password",
            )
            if self.hint:
                yield Label(self.hint, classes="hint")
            yield Label(
                "Enter to submit, Esc to cancel.",
                classes="hint",
            )

    def on_mount(self) -> None:
        # Focus the input so the user can type immediately without
        # an extra Tab.
        self.query_one("#password-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # User pressed Enter; return the value to the caller.
        password = event.value
        # Clear the widget's own reference so we don't keep the
        # plaintext alive in the ModalScreen instance itself.
        try:
            self.query_one("#password-input", Input).value = ""
        except Exception:
            pass
        self.dismiss(password if password else None)

    def action_cancel(self) -> None:
        self.dismiss(None)
