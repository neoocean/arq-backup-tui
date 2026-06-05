"""Backup-set browser (Storage Locations section).

A two-pane master-detail view:

- **Storage Locations** (left): every destination ever opened (the
  persisted ``DestinationStore``) merged read-only with a local
  Arq.app's storage locations, plus an "Add storage location" entry.
- **Backup Records** (right): on selecting a location we run
  ``arq_validator.layout.discover_layout`` and render
  ``computer-uuid → folder-uuid → backuprecord`` as a Tree; selecting a
  record pushes :class:`RecordBrowserScreen`.

The UI + logic live in :class:`StoragePanel` (a widget) so the main
shell can host it as the right-hand content of the Storage Locations
section. :class:`BackupSetListScreen` is a thin wrapper kept for direct
pushes (slash-command console, tests).
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


def merged_destinations(app):
    """Own remembered destinations merged with the read-only mirror of a
    locally-installed Arq.app's *active* storage locations.

    Own entries win on collision (matched by the
    ``(kind, host, port, user, path)`` coordinates the store keys on).
    Arq cloud locations (arqpremium / S3 / …) are skipped — they map to
    no openable local/SFTP backend (README §1). Shared by the Storage
    Locations + Validate panels so both show the same set."""
    own = app.destination_store.list()

    def key(d):
        return (d.kind, d.host, d.port, d.user, d.path)

    seen = {key(d) for d in own}
    merged = list(own)
    arq_src = getattr(app, "arq_app", None)
    if arq_src is not None:
        for sl in arq_src.storage_locations(active_only=True):
            dest = sl.to_destination()
            if dest is None or key(dest) in seen:
                continue
            seen.add(key(dest))
            merged.append(dest)
    return merged


class StoragePanel(Vertical):
    """Two-pane storage-location browser. Bindings ride on the panel so
    they work both standalone and when hosted in the main shell."""

    BINDINGS = [
        Binding("a", "add_destination", "Add Storage Location", show=True),
        Binding("v", "validate_destination", "Validate", show=True),
        Binding("m", "maintenance", "Maintenance", show=True),
        Binding("d", "delete_destination", "Delete", show=True),
    ]

    DEFAULT_CSS = """
    StoragePanel #pane-row {
        height: 1fr;
    }
    StoragePanel #destinations-pane {
        width: 40;
        border: round $primary;
        padding: 0 1;
    }
    StoragePanel #layout-pane {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    StoragePanel .pane-title {
        text-style: bold;
        margin-bottom: 1;
    }
    StoragePanel .empty-hint {
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(self, *, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self._opened_backend = None
        self._opened_dest: Optional[Destination] = None
        # Monotonic open token: results from a superseded open (operator
        # picked another location meanwhile) are ignored by their token.
        self._open_token = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="pane-row"):
            with Vertical(id="destinations-pane"):
                yield Static("Storage Locations", classes="pane-title")
                yield ListView(id="destinations-list")
            with Vertical(id="layout-pane"):
                yield Static("Backup Records", classes="pane-title")
                yield Static(
                    "Select a storage location on the left.",
                    classes="empty-hint",
                    id="layout-hint",
                )
                tree: Tree[dict] = Tree("(no destination)", id="layout-tree")
                tree.show_root = False
                tree.display = False
                yield tree

    def on_mount(self) -> None:
        self._refresh_destinations()

    def on_unmount(self) -> None:
        if self._opened_backend is not None:
            close_backend(self._opened_backend)
            self._opened_backend = None

    def _refresh_destinations(self) -> None:
        list_view = self.query_one("#destinations-list", ListView)
        list_view.clear()
        for d in self._merged_destinations():
            list_view.append(_DestinationItem(d))
        list_view.append(_AddDestinationItem())

    def _merged_destinations(self):
        return merged_destinations(self.app)

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

    def _set_status(self, text: str) -> None:
        """Show a status / error line in the right pane (hiding the
        tree). Used while a blocking open / read runs in a worker so the
        operator can see what the app is doing instead of a frozen UI."""
        hint = self.query_one("#layout-hint", Static)
        hint.update(text)
        hint.display = True
        try:
            self.query_one("#layout-tree", Tree).display = False
        except Exception:
            pass

    def _open_destination(self, dest: Destination) -> None:
        # Tear down any previously-opened backend; bump the token so
        # stale worker results are ignored.
        if self._opened_backend is not None:
            close_backend(self._opened_backend)
            self._opened_backend = None
        self._open_token += 1
        token = self._open_token
        self._opened_dest = dest
        self.app.destination_store.add_or_touch(dest)
        sftp_password = None
        if dest.kind == "sftp":
            cached = self.app.credential_cache.get_sftp_auth(dest)
            if cached:
                sftp_password = cached.get("password")
        # Connecting + reading records is blocking IO (network for
        # SFTP); run it in a worker thread and show progress so the UI
        # never freezes. The right pane reports each stage.
        self._set_status(f"Opening {dest.display()}…")
        self.run_worker(
            lambda: self._open_worker(dest, sftp_password, token),
            thread=True, exclusive=True, group="storage-open",
        )

    # -- open: connect + discover (worker thread) --------------------

    def _open_worker(self, dest, sftp_password, token) -> None:
        try:
            backend = open_backend(dest, sftp_password=sftp_password)
            layouts = discover_layout(backend, "/")
        except Exception as exc:
            self.app.call_from_thread(
                self._on_open_error, dest, str(exc), token,
            )
            return
        self.app.call_from_thread(
            self._after_open, dest, backend, layouts, token,
        )

    def _after_open(self, dest, backend, layouts, token) -> None:
        if token != self._open_token:
            close_backend(backend)
            return
        self._opened_backend = backend
        cached_pw = self.app.credential_cache.get_encryption_password(dest)
        if cached_pw is None:
            def _with_pw(pw):
                if not pw:
                    self._set_status(
                        f"{dest.display()}\n\n(Encryption password needed — "
                        "select this location again to enter it.)")
                    return
                self.app.credential_cache.set_encryption_password(dest, pw)
                self._load_records(dest, backend, pw, layouts, token)
            self.app.push_screen(
                PasswordModal(
                    prompt=f"Encryption password for {dest.display()}",
                ),
                _with_pw,
            )
        else:
            self._load_records(dest, backend, cached_pw, layouts, token)

    def _on_open_error(self, dest, msg, token) -> None:
        if token != self._open_token:
            return
        low = msg.lower()
        if dest.kind == "sftp" and any(
            s in low for s in ("permission denied", "publickey", "auth")
        ):
            # Actionable: the server rejected credentials — let the
            # operator (re)enter the SSH password right here and retry.
            self._set_status(
                f"Could not connect to {dest.display()}:\n{msg}\n\n"
                "The server rejected the credentials. A password prompt "
                "is open — enter the SSH password to retry.")

            def _retry(pw):
                if not pw:
                    return
                self.app.credential_cache.set_sftp_auth(
                    dest, {"password": pw},
                )
                self._open_destination(dest)
            self.app.push_screen(
                PasswordModal(
                    prompt=f"SSH password for {dest.user}@{dest.host}",
                ),
                _retry,
            )
        else:
            self._set_status(f"Could not open {dest.display()}:\n{msg}")

    # -- read records (worker thread) --------------------------------

    def _load_records(self, dest, backend, password, layouts, token) -> None:
        self._set_status(
            f"Reading backup records from {dest.display()}…")
        self.run_worker(
            lambda: self._records_worker(
                dest, backend, password, layouts, token,
            ),
            thread=True, exclusive=True, group="storage-records",
        )

    def _records_worker(self, dest, backend, password, layouts, token) -> None:
        rs = Restore("/", encryption_password=password, backend=backend)
        # Validate the password once up front (keyset HMAC); a wrong
        # password is recoverable in place rather than spamming
        # per-folder "keyset HMAC mismatch".
        if layouts:
            try:
                rs.keyset(layouts[0].computer_uuid)
            except Exception as exc:
                if self._is_bad_password(exc):
                    self.app.call_from_thread(
                        self._on_bad_password, dest, token,
                    )
                    return
        comps = []
        for lay in layouts:
            folders = []
            for fu in lay.backup_folder_uuids:
                try:
                    recs = rs.list_records(
                        folder_uuid=fu, computer_uuid=lay.computer_uuid,
                    )
                    folders.append({"folder_uuid": fu, "records": recs})
                except Exception as exc:
                    folders.append({"folder_uuid": fu, "error": str(exc)})
            comps.append({
                "computer_uuid": lay.computer_uuid, "folders": folders,
            })
        self.app.call_from_thread(self._render_records, dest, comps, token)

    def _on_bad_password(self, dest, token) -> None:
        if token != self._open_token:
            return
        self.app.credential_cache.forget(dest)
        self._set_status(
            f"Incorrect encryption password for {dest.display()}.\n\n"
            "A password prompt is open — enter the correct password to "
            "retry.")

        def _retry(pw):
            if not pw:
                return
            self.app.credential_cache.set_encryption_password(dest, pw)
            self._open_destination(dest)
        self.app.push_screen(
            PasswordModal(
                prompt=f"Encryption password for {dest.display()}",
            ),
            _retry,
        )

    # -- render (main thread, no IO) ---------------------------------

    def _render_records(self, dest, comps, token) -> None:
        if token != self._open_token:
            return
        self.query_one("#layout-hint").display = False
        tree = self.query_one("#layout-tree", Tree)
        tree.display = True
        tree.clear()
        tree.root.label = dest.display()
        tree.show_root = True
        if not comps:
            tree.root.add_leaf("(no Arq computer trees found)")
            return
        comp_node = None
        for comp in comps:
            comp_node = tree.root.add(
                comp["computer_uuid"],
                data={
                    "kind": "computer",
                    "computer_uuid": comp["computer_uuid"],
                },
            )
            for fld in comp["folders"]:
                fu = fld["folder_uuid"]
                folder_node = comp_node.add(
                    fu,
                    data={
                        "kind": "folder",
                        "computer_uuid": comp["computer_uuid"],
                        "folder_uuid": fu,
                    },
                )
                if "error" in fld:
                    folder_node.add_leaf(
                        f"(error reading records: {fld['error']})",
                    )
                    continue
                records = fld["records"]
                if not records:
                    folder_node.add_leaf("(no records)")
                    continue
                # Newest first; tag the newest "[latest]".
                for i, rec in enumerate(reversed(records)):
                    folder_node.add_leaf(
                        self._record_label(rec, is_latest=(i == 0)),
                        data={
                            "kind": "record",
                            "computer_uuid": comp["computer_uuid"],
                            "folder_uuid": fu,
                            "relative_path": rec.relative_path,
                            "creation_date": rec.creation_date,
                        },
                    )
        if comp_node is not None:
            comp_node.expand_all()
        tree.root.expand()

    @staticmethod
    def _is_bad_password(exc: Exception) -> bool:
        """Whether ``exc`` from keyset decryption signals a wrong
        password (vs. a corruption / IO error). The keyset path raises
        with a 'keyset HMAC mismatch — wrong password OR …' message."""
        msg = str(exc).lower()
        return any(
            s in msg for s in ("hmac mismatch", "wrong password", "keyset")
        )

    @staticmethod
    def _record_label(rec, *, is_latest: bool = False) -> str:
        if rec.creation_date:
            ts = datetime.datetime.fromtimestamp(rec.creation_date)
            stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        else:
            stamp = "(unknown)"
        latest = "  [latest]" if is_latest else ""
        complete = "" if rec.is_complete else " [INCOMPLETE]"
        return f"{stamp}{latest}{complete}"

    # ------------------------------------------------------------------
    # Validate / maintenance / open record
    # ------------------------------------------------------------------

    def action_validate_destination(self) -> None:
        if self._opened_backend is None or self._opened_dest is None:
            self.notify(
                "Open a storage location first.", severity="warning",
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
        for the currently-opened storage location. Requires both the
        backend and an already-cached encryption password."""
        if self._opened_backend is None or self._opened_dest is None:
            self.notify(
                "Open a storage location first.", severity="warning",
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

    def action_delete_destination(self) -> None:
        """Remove the focused storage location from the list (after a
        confirmation). Only own (manually-added) locations can be
        removed here; Arq-mirrored ones are read-only. Removing never
        deletes the backup data on disk — it just forgets the entry."""
        list_view = self.query_one("#destinations-list", ListView)
        idx = getattr(list_view, "index", None)
        if idx is None or idx < 0:
            return
        try:
            item = list_view.children[idx]
        except IndexError:
            return
        if not isinstance(item, _DestinationItem):
            return  # the "Add storage location" row
        dest = item.dest
        if dest.origin == "arq":
            self.notify(
                f"'{dest.display()}' is managed by Arq.app — remove it "
                "in Arq, not here.",
                severity="warning",
            )
            return
        from ..widgets.confirm_modal import ConfirmModal

        def _confirmed(ok: bool) -> None:
            if not ok:
                return
            self.app.destination_store.remove(dest)
            # If the removed location was the open one, reset the right
            # pane back to the placeholder.
            if (self._opened_dest is not None
                    and self._opened_dest.display() == dest.display()):
                if self._opened_backend is not None:
                    close_backend(self._opened_backend)
                    self._opened_backend = None
                self._opened_dest = None
                self._open_token += 1
                try:
                    self.query_one("#layout-tree", Tree).display = False
                    hint = self.query_one("#layout-hint", Static)
                    hint.update("Select a storage location on the left.")
                    hint.display = True
                except Exception:
                    pass
            self._refresh_destinations()
        self.app.push_screen(
            ConfirmModal(
                title="Delete storage location",
                message=(
                    f"Remove '{dest.display()}' from the list?\n\n"
                    "This only forgets it here — the backup data on the "
                    "destination is NOT deleted."
                ),
            ),
            _confirmed,
        )

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


class BackupSetListScreen(Screen):
    """Standalone wrapper around :class:`StoragePanel` — kept for direct
    pushes (slash-command console, tests). The main shell hosts the
    panel directly instead."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield StoragePanel(id="storage-panel")
        yield Footer()


class _DestinationItem(ListItem):
    def __init__(self, dest: Destination) -> None:
        badge = "◆ Arq  " if dest.origin == "arq" else ""
        super().__init__(Static(f"{badge}{dest.display()}"))
        self.dest = dest


class _AddDestinationItem(ListItem):
    def __init__(self) -> None:
        super().__init__(Static("[ + Add storage location ]"))
