"""Record browser — walk the tree inside one backuprecord.

Lazy walk: the root tree blob is fetched + parsed on screen mount;
each TreeNode child is added as a still-collapsed Tree node
carrying enough state in its ``data`` field to fetch its own
subtree on first expansion. Parsed Tree blobs are cached by
``blobIdentifier`` so re-collapsing + re-expanding doesn't refetch.

The right-hand metadata pane reflects whichever node is currently
focused.
"""

from __future__ import annotations

import datetime
import plistlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, Tree

from arq_reader.decrypt import decrypt_encrypted_object, decrypt_lz4_arqo
from arq_reader.parse import parse_tree
from arq_validator.crypto import decrypt_keyset
from arq_writer.lz4_block import lz4_unwrap
from arq_writer.types import BlobLoc, FileNode, TreeNode


@dataclass
class _NodeState:
    kind: str            # "tree", "file", "loading"
    rel_path: str
    file_node: Optional[FileNode] = None
    tree_loc: Optional[BlobLoc] = None
    expanded: bool = False


class RecordBrowserScreen(Screen):
    """Walks the tree inside one backuprecord."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("q", "app.quit", "Quit", show=True),
        Binding("space", "toggle_mark", "Mark / unmark", show=True),
        Binding("R", "restore_full", "Restore full record", show=True),
        Binding("r", "restore_selected", "Restore marked", show=True),
    ]

    DEFAULT_CSS = """
    RecordBrowserScreen #record-row {
        height: 1fr;
    }
    RecordBrowserScreen #tree-pane {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    RecordBrowserScreen #meta-pane {
        width: 50;
        border: round $primary;
        padding: 0 1;
    }
    RecordBrowserScreen .pane-title {
        text-style: bold;
        margin-bottom: 1;
    }
    RecordBrowserScreen #meta-content {
        height: 1fr;
    }
    """

    def __init__(
        self, *,
        backend,
        dest_label: str,
        password: str,
        computer_uuid: str,
        folder_uuid: str,
        backuprecord_path: str,
        creation_date: int,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.dest_label = dest_label
        self.password = password
        self.computer_uuid = computer_uuid
        self.folder_uuid = folder_uuid
        self.backuprecord_path = backuprecord_path
        self.creation_date = creation_date
        # Tree blobs are cached by blob_id so re-expanding a node
        # is free.
        self._tree_cache: Dict[str, Any] = {}
        self._keyset = None
        # Set of source-relative paths the user marked for selective
        # restore. Iteration order doesn't matter — Restore takes
        # an unordered list and the path filter normalizes it.
        self._marked_paths: set = set()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="record-row"):
            with Vertical(id="tree-pane"):
                yield Static(
                    self._title(), classes="pane-title",
                )
                tree: Tree[_NodeState] = Tree("/", id="record-tree")
                tree.show_root = True
                yield tree
            with Vertical(id="meta-pane"):
                yield Static("Selected", classes="pane-title")
                yield Static(
                    "Move with arrow keys to inspect a node.",
                    id="meta-content",
                )
        yield Footer()

    def _title(self) -> str:
        if self.creation_date:
            ts = datetime.datetime.fromtimestamp(self.creation_date)
            stamp = ts.strftime("%Y-%m-%d %H:%M:%S")
        else:
            stamp = "(unknown date)"
        return f"{stamp} — {self.dest_label}"

    # ------------------------------------------------------------------
    # Initial load
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        try:
            self._load_root()
        except Exception as exc:
            self.notify(
                f"Could not load backuprecord: {exc}",
                severity="error",
            )

    def _load_root(self) -> None:
        # Decrypt the keyset under our destination's password (cache
        # the result so each tree blob doesn't re-derive the key).
        keyset_path = f"/{self.computer_uuid}/encryptedkeyset.dat"
        keyset_blob = self.backend.read_all(keyset_path)
        self._keyset = decrypt_keyset(keyset_blob, self.password)

        # Decrypt the backuprecord plist to get the root node.
        record_arqo = self.backend.read_all(self.backuprecord_path)
        record_plain = decrypt_lz4_arqo(
            record_arqo,
            self._keyset.encryption_key, self._keyset.hmac_key,
        )
        # Dual-format: writer now defaults to JSON, but we accept
        # the legacy binary plist destinations operators may still
        # have on disk.
        from arq_writer.backuprecord import parse_backuprecord
        record = parse_backuprecord(record_plain)
        root_node_dict = record.get("node")
        if not isinstance(root_node_dict, dict):
            raise ValueError(
                "backuprecord missing or malformed `node` field"
            )

        tree_widget = self.query_one("#record-tree", Tree)
        tree_widget.clear()
        if root_node_dict.get("isTree"):
            tloc = _blobloc_from_dict(
                root_node_dict.get("treeBlobLoc") or {}
            )
            tree_widget.root.label = "/"
            tree_widget.root.data = _NodeState(
                kind="tree", rel_path="", tree_loc=tloc,
            )
            tree_widget.root.expand()
            self._expand_tree_node(tree_widget.root)
        else:
            file_node = _file_node_from_dict(root_node_dict)
            tree_widget.root.label = "(file root)"
            tree_widget.root.data = _NodeState(
                kind="file", rel_path="", file_node=file_node,
            )

    # ------------------------------------------------------------------
    # Lazy expansion
    # ------------------------------------------------------------------

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        state = event.node.data
        if not isinstance(state, _NodeState):
            return
        if state.kind != "tree" or state.expanded:
            return
        self._expand_tree_node(event.node)

    def _expand_tree_node(self, node) -> None:
        state: _NodeState = node.data
        if state.expanded or state.tree_loc is None:
            return
        state.expanded = True
        try:
            tree_obj = self._fetch_tree(state.tree_loc)
        except Exception as exc:
            node.add_leaf(f"(error: {exc})")
            return
        for child in tree_obj.children:
            child_rel = (
                f"{state.rel_path}/{child.name}"
                if state.rel_path else child.name
            )
            if isinstance(child.node, TreeNode):
                node.add(
                    child.name,
                    data=_NodeState(
                        kind="tree",
                        rel_path=child_rel,
                        tree_loc=child.node.treeBlobLoc,
                    ),
                )
            elif isinstance(child.node, FileNode):
                node.add_leaf(
                    child.name,
                    data=_NodeState(
                        kind="file",
                        rel_path=child_rel,
                        file_node=child.node,
                    ),
                )

    def _fetch_tree(self, loc: BlobLoc):
        cached = self._tree_cache.get(loc.blobIdentifier)
        if cached is not None:
            return cached
        if loc.isPacked:
            raw = self.backend.read_range(
                loc.relativePath, loc.offset, loc.length,
            )
        else:
            raw = self.backend.read_all(loc.relativePath)
        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, self._keyset.encryption_key,
                self._keyset.hmac_key,
            )
        if loc.compressionType == 2:
            raw = lz4_unwrap(raw)
        tree_obj = parse_tree(raw)
        self._tree_cache[loc.blobIdentifier] = tree_obj
        return tree_obj

    # ------------------------------------------------------------------
    # Metadata pane
    # ------------------------------------------------------------------

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        state = event.node.data
        meta = self.query_one("#meta-content", Static)
        if not isinstance(state, _NodeState):
            meta.update("(no selection)")
            return
        meta.update(self._format_meta(event.node, state))

    # ------------------------------------------------------------------
    # Mark / restore
    # ------------------------------------------------------------------

    def action_toggle_mark(self) -> None:
        tree = self.query_one("#record-tree", Tree)
        node = tree.cursor_node
        if node is None:
            return
        state = node.data
        if not isinstance(state, _NodeState):
            return
        if not state.rel_path:
            self.notify(
                "Select a child node to mark; root is always full.",
                severity="warning",
            )
            return
        if state.rel_path in self._marked_paths:
            self._marked_paths.discard(state.rel_path)
            node.label = self._strip_mark(node.label)
        else:
            self._marked_paths.add(state.rel_path)
            node.label = self._add_mark(node.label)

    def action_restore_full(self) -> None:
        self._launch_restore(paths=None)

    def action_restore_selected(self) -> None:
        if not self._marked_paths:
            self.notify(
                "No items marked. Use Space to mark first.",
                severity="warning",
            )
            return
        self._launch_restore(paths=sorted(self._marked_paths))

    def _launch_restore(self, *, paths) -> None:
        from ..widgets.restore_target_modal import RestoreTargetModal
        from .restore_run import RestoreRunScreen

        if paths is None:
            summary = f"Restoring full record from {self.dest_label}"
        else:
            summary = (
                f"Restoring {len(paths)} marked path"
                f"{'s' if len(paths) != 1 else ''} from "
                f"{self.dest_label}"
            )

        def _go(target):
            if not target:
                return
            from pathlib import Path as _P
            self.app.push_screen(RestoreRunScreen(
                backend=self.backend,
                encryption_password=self.password,
                computer_uuid=self.computer_uuid,
                folder_uuid=self.folder_uuid,
                backuprecord_path=self.backuprecord_path,
                target=_P(target),
                paths=paths,
                record_label=self._title(),
            ))

        self.app.push_screen(
            RestoreTargetModal(summary=summary), _go,
        )

    @staticmethod
    def _add_mark(label) -> str:
        s = str(label)
        if s.startswith("[★] "):
            return s
        return f"[★] {s}"

    @staticmethod
    def _strip_mark(label) -> str:
        s = str(label)
        if s.startswith("[★] "):
            return s[len("[★] "):]
        return s

    def _format_meta(self, node, state: _NodeState) -> str:
        lines = [
            f"path: /{state.rel_path}" if state.rel_path else "path: /",
            f"kind: {state.kind}",
        ]
        if state.kind == "file" and state.file_node is not None:
            fn = state.file_node
            lines.append(f"size: {fn.itemSize}")
            if fn.mtime_sec:
                ts = datetime.datetime.fromtimestamp(fn.mtime_sec)
                lines.append(
                    f"mtime: {ts.strftime('%Y-%m-%d %H:%M:%S')}"
                    f".{fn.mtime_nsec:09d}"
                )
            lines.append(f"mode: 0o{fn.mac_st_mode & 0o7777:04o}")
            lines.append(f"chunks: {len(fn.dataBlobLocs)}")
            for i, loc in enumerate(fn.dataBlobLocs):
                kind = "packed" if loc.isPacked else "standalone"
                lines.append(
                    f"  [{i}] {kind} {loc.blobIdentifier[:16]}... "
                    f"len={loc.length}"
                )
        elif state.kind == "tree" and state.tree_loc is not None:
            tl = state.tree_loc
            lines.append(f"tree blob: {tl.blobIdentifier[:32]}...")
            lines.append(f"  packed: {tl.isPacked}")
            lines.append(f"  length: {tl.length}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plist Node-dict helpers (the writer's backuprecord encodes Node as a dict)
# ---------------------------------------------------------------------------


def _blobloc_from_dict(d: Dict[str, Any]) -> BlobLoc:
    return BlobLoc(
        blobIdentifier=str(d.get("blobIdentifier") or ""),
        isPacked=bool(d.get("isPacked", False)),
        relativePath=str(d.get("relativePath") or ""),
        offset=int(d.get("offset") or 0),
        length=int(d.get("length") or 0),
        stretchEncryptionKey=bool(d.get("stretchEncryptionKey", True)),
        compressionType=int(d.get("compressionType") or 2),
    )


def _file_node_from_dict(d: Dict[str, Any]) -> FileNode:
    return FileNode(
        dataBlobLocs=[
            _blobloc_from_dict(b)
            for b in d.get("dataBlobLocs") or []
        ],
        itemSize=int(d.get("itemSize") or 0),
        mtime_sec=int(d.get("modificationTime_sec") or 0),
        mtime_nsec=int(d.get("modificationTime_nsec") or 0),
        ctime_sec=int(d.get("changeTime_sec") or 0),
        ctime_nsec=int(d.get("changeTime_nsec") or 0),
        mac_st_mode=int(d.get("mac_st_mode") or 0),
    )
