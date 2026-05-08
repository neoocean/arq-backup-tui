"""Multi-source picker.

A list of source paths the plan wizard accumulates. Add via an
Input + button; remove via a key binding on the focused row.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Input, ListItem, ListView, Static


class SourcePicker(Widget):
    """Visible list + add/remove controls for source paths.

    Caller reads :attr:`paths` to obtain the current selection.
    Paths are stored verbatim — callers that need
    ``Path.expanduser`` should apply it themselves at submit time.
    """

    BINDINGS = [
        Binding("delete", "remove_focused", "Remove", show=False),
    ]

    DEFAULT_CSS = """
    SourcePicker {
        height: auto;
    }
    SourcePicker .row {
        height: 3;
        align: left middle;
    }
    SourcePicker Input {
        width: 1fr;
    }
    SourcePicker ListView {
        height: auto;
        max-height: 8;
        border: round $primary;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.paths: List[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="row"):
            yield Input(
                placeholder="/path/to/source",
                id="source-input",
            )
            yield Button("Add", id="add-source", variant="primary")
        yield ListView(id="source-list")
        yield Static(
            "Press Delete on a row to remove it.",
            classes="hint",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-source":
            self._add_from_input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "source-input":
            self._add_from_input()

    def _add_from_input(self) -> None:
        inp = self.query_one("#source-input", Input)
        raw = inp.value.strip()
        if not raw:
            return
        path = str(Path(raw).expanduser())
        if path in self.paths:
            self.notify("Already added.", severity="warning")
            return
        self.paths.append(path)
        list_view = self.query_one("#source-list", ListView)
        list_view.append(ListItem(Static(path)))
        inp.value = ""
        inp.focus()

    def action_remove_focused(self) -> None:
        list_view = self.query_one("#source-list", ListView)
        idx = list_view.index
        if idx is None or idx < 0 or idx >= len(self.paths):
            return
        del self.paths[idx]
        list_view.remove_items([idx])
