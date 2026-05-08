"""Modal for adding a new destination (local or SFTP).

Returns a :class:`~arq_tui.state.Destination` (and, for SFTP, an
auth payload that the caller stashes in
:class:`~arq_tui.state.CredentialCache`) via
``ModalScreen.dismiss``.
"""

from __future__ import annotations

from typing import Optional, Tuple

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet

from ..state import Destination


class DestinationModal(ModalScreen[Optional[Tuple[Destination, dict]]]):
    """Open a destination by typing its coordinates.

    Returns ``(Destination, auth)`` on submit, ``None`` on cancel.
    ``auth`` is a dict with optional keys ``password`` /
    ``identity_file`` for SFTP destinations; empty for local.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    DestinationModal {
        align: center middle;
    }
    DestinationModal > Vertical {
        width: 80;
        max-width: 95%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    DestinationModal Label.title {
        text-style: bold;
        margin-bottom: 1;
    }
    DestinationModal .field-label {
        color: $text-muted;
        margin-top: 1;
    }
    DestinationModal #buttons {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    DestinationModal Button {
        margin: 0 0 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Add backup destination", classes="title")
            yield Label("Kind", classes="field-label")
            with RadioSet(id="kind-set"):
                yield RadioButton("Local filesystem", value=True, id="kind-local")
                yield RadioButton("SFTP", id="kind-sftp")

            yield Label("Label (optional)", classes="field-label")
            yield Input(placeholder="e.g. nas-mirror", id="label")

            yield Label("Local path (kind=Local)", classes="field-label")
            yield Input(
                placeholder="/Volumes/arqbackup1", id="local-path",
            )

            yield Label("SFTP host (kind=SFTP)", classes="field-label")
            yield Input(placeholder="example.com", id="sftp-host")
            yield Label("SFTP user", classes="field-label")
            yield Input(placeholder="u123", id="sftp-user")
            yield Label("SFTP port", classes="field-label")
            yield Input(value="22", id="sftp-port")
            yield Label("Remote root path", classes="field-label")
            yield Input(
                placeholder="/home/u123/arq", id="sftp-root",
            )
            yield Label(
                "Identity file (leave blank for password auth)",
                classes="field-label",
            )
            yield Input(
                placeholder="~/.ssh/id_ed25519", id="sftp-identity",
            )

            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Open", id="submit", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "submit":
            self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        kind = "sftp" if self.query_one(
            "#kind-sftp", RadioButton,
        ).value else "local"
        label = self.query_one("#label", Input).value.strip()
        if kind == "local":
            path = self.query_one("#local-path", Input).value.strip()
            if not path:
                self.notify("Local path is required.", severity="error")
                return
            dest = Destination(kind="local", label=label, path=path)
            self.dismiss((dest, {}))
            return
        # SFTP
        host = self.query_one("#sftp-host", Input).value.strip()
        user = self.query_one("#sftp-user", Input).value.strip()
        port_text = self.query_one("#sftp-port", Input).value.strip() or "22"
        root = self.query_one("#sftp-root", Input).value.strip()
        identity = self.query_one("#sftp-identity", Input).value.strip()
        if not host or not user or not root:
            self.notify(
                "SFTP host, user, and root are required.",
                severity="error",
            )
            return
        try:
            port = int(port_text)
        except ValueError:
            self.notify("Port must be a number.", severity="error")
            return
        dest = Destination(
            kind="sftp", label=label, host=host, user=user,
            port=port, path=root, identity_file=identity,
        )
        auth = {"identity_file": identity} if identity else {}
        self.dismiss((dest, auth))
