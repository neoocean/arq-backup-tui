"""Left-rail sidebar widget for the Arq-7-styled TUI.

Mirrors the sidebar in Arq.app's macOS GUI: a fixed-width
column listing the main sections (Plans, Activity, Browse,
Validate, Help). One section is "active" at any time; clicking
or focusing a section emits a :class:`SidebarNavigation` message
the screen handler routes to ``app.push_screen`` for that
section.

Operators familiar with Arq.app find this layout immediately
recognisable. Operators who prefer the keyboard-only flat
layout keep using the existing HomeScreen — the sidebar is
opt-in per-screen via ``compose()``.

Why a separate widget rather than baking it into HomeScreen:
the same sidebar appears on every section's screen so the
operator can jump directly between Plans → Activity → Browse
without navigating back. Wrapping it as one widget keeps the
section list defined once + propagates the highlighted-active
state automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from textual.containers import Vertical
from textual import events
from textual.message import Message
from textual.widgets import Static


# Sections we surface in the sidebar. Order matters — it's the
# top-to-bottom display order. Each tuple is ``(key, label)``;
# ``key`` is the SidebarNavigation.section value the screen
# handler routes on.
DEFAULT_SECTIONS: List[Tuple[str, str]] = [
    ("plans", "Plans"),
    ("activity", "Activity"),
    ("browse", "Browse"),
    ("validate", "Validate"),
    ("help", "Help"),
]


def section_for_screen(screen_class_name: str) -> str:
    """Map a Screen subclass name to the sidebar section it
    represents. Used by screens that adopt the Sidebar so the
    active highlight stays in lockstep as the operator navigates
    between sections.

    Unknown / unmatched screens default to ``plans`` (the
    landing screen) — at worst the highlight is wrong on a
    rare screen rather than missing entirely."""
    routing = {
        "HomeScreen": "plans",
        "RunsMonitorScreen": "activity",
        "BackupSetListScreen": "browse",
        "RecordBrowserScreen": "browse",
        "ValidateLaunchScreen": "validate",
        "ValidateRunScreen": "validate",
        "HelpScreen": "help",
        "SchedulingScreen": "plans",
    }
    return routing.get(screen_class_name, "plans")


class SidebarNavigation(Message):
    """Posted when the operator selects a section in the sidebar.

    The screen handler routes this to ``app.push_screen`` (or a
    no-op when the active section is re-selected). ``section`` is
    one of the keys in :data:`DEFAULT_SECTIONS`."""

    def __init__(self, section: str) -> None:
        super().__init__()
        self.section = section


class Sidebar(Vertical):
    """Left-rail navigation. Apply the ``sidebar`` CSS class so
    the global theme picks up the styling.

    The active section is set via :meth:`set_active`; it
    re-applies the ``-active`` class to highlight the row
    matching the screen the operator is currently on.
    """

    DEFAULT_CSS = ""    # actual styling lives in theming.css
    DEFAULT_CLASSES = "sidebar"

    def __init__(
        self,
        *,
        title: str = "arq-backup-tui",
        sections: Optional[List[Tuple[str, str]]] = None,
        active: str = "plans",
    ) -> None:
        super().__init__()
        self._title = title
        self._sections = sections or DEFAULT_SECTIONS
        self._active = active

    def compose(self):
        yield Static(self._title, classes="sidebar-title")
        for key, label in self._sections:
            cls = "sidebar-section"
            if key == self._active:
                cls += " -active"
            yield Static(
                label, classes=cls, id=f"sidebar-{key}",
            )

    async def on_click(self, event: events.Click) -> None:
        """Mouse click on a sidebar row → emit
        :class:`SidebarNavigation` so the screen handler can
        route to the matching section. Hosts that bind
        on_sidebar_navigation get one event per click; hosts
        that don't see no effect (no exception)."""
        # Walk up the click target's ancestry until we find a
        # row with the sidebar-section-keyed id ("sidebar-…").
        widget = event.widget
        while widget is not None:
            wid = getattr(widget, "id", None)
            if wid and wid.startswith("sidebar-"):
                section_key = wid[len("sidebar-"):]
                self.post_message(SidebarNavigation(section_key))
                self.set_active(section_key)
                event.stop()
                return
            widget = getattr(widget, "parent", None)

    def set_active(self, section: str) -> None:
        """Re-render the highlight to point at ``section``.

        Updates the ``-active`` class on each section's Static
        without rebuilding the whole widget tree."""
        for key, _label in self._sections:
            row = self.query_one(f"#sidebar-{key}", Static)
            if key == section:
                row.add_class("-active")
            else:
                row.remove_class("-active")
        self._active = section
