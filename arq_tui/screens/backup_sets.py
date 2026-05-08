"""Backup-set browser.

Top-level screen split into two panes:

- **Destinations** (left): every destination ever opened lives in
  the persisted ``DestinationStore`` plus an "Add destination"
  entry that opens the destination modal.
- **Layout tree** (right): on selecting a destination we run
  ``arq_validator.layout.discover_layout`` and render
  ``computer-uuid → folder-uuid → backuprecord`` as a Tree. Each
  record's chronological label comes from
  ``Restore.list_records``.

Selecting a backuprecord pushes :class:`RecordBrowserScreen`
(see ``record_browser.py``) which lets the user walk the tree
inside that record.
"""

from __future__ import annotations

import datetime
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, ListItem, ListView, Static, Tree

from arq_reader import Restore
from arq_validator.layout import discover_layout

from ..backend_open import close_backend, open_backend
from ..state import Destination
from ..widgets.destination_modal import DestinationModal
from ..widgets.password_modal import PasswordModal
from .record_browser import RecordBrowserScreen


class BackupSetListScreen(Screen):
    """Two-pane backup-set browser."""

    BINDINGS = [
        Binding("a", "add_destination", "Add destination", show=True),
        Binding("v", "validate_destination", "Validate", show=True),
        Binding("m", "maintenance", "Maintenance", show=True),
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    DEFAULT_CSS = """
    BackupSetListScreen #pane-row {
        height: 1fr;
    }
    BackupSetListScreen #destinations-pane {
        width: 40;
        border: round $primary;
        padding: 0 1;
    }
    BackupSetListScreen #layout-pane {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    BackupSetListScreen .pane-title {
        text-style: bold;
        margin-bottom: 1;
    }
    BackupSetListScreen .empty-hint {
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._opened_backend = None
        self._opened_dest: Optional[Destination] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="pane-row"):
            with Vertical(id="destinations-pane"):
                yield Static("Destinations", classes="pane-title")
                yield ListView(id="destinations-list")
            with Vertical(id="layout-pane"):
                yield Static("Layout", classes="pane-title")
                yield Static(
                    "Select a destination on the left.",
                    classes="empty-hint",
                    id="layout-hint",
                )
                tree: Tree[dict] = Tree("(no destination)", id="layout-tree")
                tree.show_root = False
                tree.display = False
                yield tree
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_destinations()

    def on_unmount(self) -> None:
        if self._opened_backend is not None:
            close_backend(self._opened_backend)
            self._opened_backend = None

    def _refresh_destinations(self) -> None:
        list_view = self.query_one("#destinations-list", ListView)
        list_view.clear()
        for d in self.app.destination_store.list():
            list_view.append(_DestinationItem(d))
        list_view.append(_AddDestinationItem())

    # ------------------------------------------------------------------
    # Add destination flow
    # ------------------------------------------------------------------

    def action_add_destination(self) -> None:
        def _opened(result):
            if result is None:
                return
            dest, auth = result
            if dest.kind == "sftp" and not dest.identity_file:
                # Need a password for SFTP password auth — chain a
                # password modal in.
                def _with_password(pw):
                    if not pw:
                        return
                    self.app.credential_cache.set_sftp_auth(
                        dest, {"password": pw},
                    )
                    self._after_destination_added(dest, auth)
                self.app.push_screen(
                    PasswordModal(
                        prompt=f"SSH password for {dest.user}@{dest.host}",
                    ),
                    _with_password,
                )
            else:
                if auth:
                    self.app.credential_cache.set_sftp_auth(dest, auth)
                self._after_destination_added(dest, auth)
        self.app.push_screen(DestinationModal(), _opened)

    def _after_destination_added(
        self, dest: Destination, auth: dict,
    ) -> None:
        self.app.destination_store.add_or_touch(dest)
        self._refresh_destinations()
        self._open_destination(dest)

    # ------------------------------------------------------------------
    # Open + render destination
    # ------------------------------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, _AddDestinationItem):
            self.action_add_destination()
            return
        if isinstance(item, _DestinationItem):
            self._open_destination(item.dest)

    def _open_destination(self, dest: Destination) -> None:
        # Tear down a previously-opened backend if the user is
        # switching between destinations.
        if self._opened_backend is not None:
            close_backend(self._opened_backend)
            self._opened_backend = None
        sftp_password = None
        if dest.kind == "sftp":
            cached = self.app.credential_cache.get_sftp_auth(dest)
            if cached:
                sftp_password = cached.get("password")
        try:
            backend = open_backend(dest, sftp_password=sftp_password)
        except Exception as exc:
            self.notify(
                f"Could not open {dest.display()}: {exc}",
                severity="error",
            )
            return
        self._opened_backend = backend
        self._opened_dest = dest
        self.app.destination_store.add_or_touch(dest)

        layouts = discover_layout(backend, "/")
        # Get the user's encryption password — needed to enumerate
        # record metadata. If we don't have it cached, prompt now.
        cached_pw = self.app.credential_cache.get_encryption_password(dest)
        if cached_pw is None:
            def _with_pw(pw):
                if not pw:
                    return
                self.app.credential_cache.set_encryption_password(dest, pw)
                self._populate_layout_tree(dest, backend, pw, layouts)
            self.app.push_screen(
                PasswordModal(
                    prompt=f"Encryption password for {dest.display()}",
                ),
                _with_pw,
            )
        else:
            self._populate_layout_tree(dest, backend, cached_pw, layouts)

    def _populate_layout_tree(
        self, dest: Destination, backend, password: str, layouts,
    ) -> None:
        # Hide the placeholder, show the tree.
        self.query_one("#layout-hint").display = False
        tree = self.query_one("#layout-tree", Tree)
        tree.display = True
        tree.clear()
        tree.root.label = dest.display()
        tree.show_root = True
        if not layouts:
            tree.root.add_leaf("(no Arq computer trees found)")
            return
        rs = Restore("/", encryption_password=password, backend=backend)
        for lay in layouts:
            comp_node = tree.root.add(
                f"{lay.computer_uuid}",
                data={"kind": "computer", "computer_uuid": lay.computer_uuid},
            )
            for folder_uuid in lay.backup_folder_uuids:
                folder_node = comp_node.add(
                    folder_uuid,
                    data={
                        "kind": "folder",
                        "computer_uuid": lay.computer_uuid,
                        "folder_uuid": folder_uuid,
                    },
                )
                try:
                    records = rs.list_records(
                        folder_uuid=folder_uuid,
                        computer_uuid=lay.computer_uuid,
                    )
                except Exception as exc:
                    folder_node.add_leaf(
                        f"(error reading records: {exc})",
                    )
                    continue
                if not records:
                    folder_node.add_leaf("(no records)")
                    continue
                # Newest first in the UI even though list_records
                # returns oldest-first.
                for rec in reversed(records):
                    label = self._record_label(rec)
                    folder_node.add_leaf(
                        label,
                        data={
                            "kind": "record",
                            "computer_uuid": lay.computer_uuid,
                            "folder_uuid": folder_uuid,
                            "relative_path": rec.relative_path,
                            "creation_date": rec.creation_date,
                        },
                    )
        comp_node.expand_all()
        tree.root.expand()

    @staticmethod
    def _record_label(rec) -> str:
        if rec.creation_date:
            ts = datetime.datetime.fromtimestamp(rec.creation_date)
            stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        else:
            stamp = "(unknown)"
        complete = "" if rec.is_complete else " [INCOMPLETE]"
        return f"{stamp}{complete}"

    # ------------------------------------------------------------------
    # Open record on selection
    # ------------------------------------------------------------------

    def action_validate_destination(self) -> None:
        if self._opened_backend is None or self._opened_dest is None:
            self.notify(
                "Open a destination first.", severity="warning",
            )
            return
        from .validate_run import ValidateLaunchScreen
        password = self.app.credential_cache.get_encryption_password(
            self._opened_dest,
        )
        self.app.push_screen(ValidateLaunchScreen(
            backend=self._opened_backend,
            password=password,
            dest_label=self._opened_dest.display(),
            config_dir=self.app.destination_store.config_dir,
        ))

    def action_maintenance(self) -> None:
        """Open the maintenance console (password rotation + retention)
        for the currently-opened destination.

        Requires both the backend and an already-cached encryption
        password — the rotation step needs both the old keyset and
        the cached password to be present, and the retention step
        needs to decrypt every backuprecord. Surfaces a notification
        if either is missing rather than re-prompting mid-flow.
        """
        if self._opened_backend is None or self._opened_dest is None:
            self.notify(
                "Open a destination first.", severity="warning",
            )
            return
        password = self.app.credential_cache.get_encryption_password(
            self._opened_dest,
        )
        if password is None:
            self.notify(
                "Encryption password not cached — open a record "
                "first to enter it.",
                severity="warning",
            )
            return
        from .maintenance import MaintenanceScreen
        self.app.push_screen(MaintenanceScreen(
            backend=self._opened_backend,
            dest=self._opened_dest,
            password=password,
        ))

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not isinstance(data, dict):
            return
        if data.get("kind") != "record":
            return
        if self._opened_backend is None or self._opened_dest is None:
            return
        password = self.app.credential_cache.get_encryption_password(
            self._opened_dest,
        )
        if password is None:
            return
        self.app.push_screen(RecordBrowserScreen(
            backend=self._opened_backend,
            dest_label=self._opened_dest.display(),
            password=password,
            computer_uuid=data["computer_uuid"],
            folder_uuid=data["folder_uuid"],
            backuprecord_path=data["relative_path"],
            creation_date=data.get("creation_date") or 0,
        ))


class _DestinationItem(ListItem):
    def __init__(self, dest: Destination) -> None:
        super().__init__(Static(dest.display()))
        self.dest = dest


class _AddDestinationItem(ListItem):
    def __init__(self) -> None:
        super().__init__(Static("[ + Add destination ]"))
