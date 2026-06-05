"""Shared base for flow screens shown as popup overlays.

These are the screens reached *from* the shell (plan wizard, backup /
restore / validate runs, record browser, maintenance, scheduling). As
``ModalScreen`` subclasses they render *over* the shell (the sidebar +
content stay visible, dimmed) instead of replacing it, and their content
is laid out inside a centered bordered ``.overlay-box`` so the popup is
clearly a window on top.

A screen adopts this by subclassing :class:`OverlayScreen` and wrapping
its content in ``with Vertical(classes="overlay-box"):`` (dropping the
app ``Header`` — the box carries its own title). The screen's own
``DEFAULT_CSS`` still applies; this base just adds the scrim + box.
"""

from __future__ import annotations

from textual.screen import ModalScreen


class OverlayScreen(ModalScreen[None]):
    """A flow screen rendered as a centered popup over the shell."""

    DEFAULT_CSS = """
    OverlayScreen {
        align: center middle;
        /* Translucent scrim so the shell behind stays visible. */
        background: $background 55%;
    }
    OverlayScreen .overlay-box {
        width: 90%;
        max-width: 140;
        height: auto;
        max-height: 90%;
        /* Tall forms scroll inside the box rather than overflow it. */
        overflow-y: auto;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    """
