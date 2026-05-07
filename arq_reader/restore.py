"""High-level restore orchestrator.

Walks a backup destination produced by :mod:`arq_writer` (or any other
backup that conforms to the same standalone-objects layout) and
materializes the tree under ``dest``.

Workflow:

    1. Discover the computer UUID, decrypt ``encryptedkeyset.dat``
       using the user-supplied password (``arq_validator.crypto``
       handles the inverse).
    2. List backup folders, choose one, find its latest backuprecord.
    3. Decrypt + LZ4-unwrap + plist-parse the backuprecord. Read its
       ``node`` field (a dict) — that's the root of the file tree.
    4. Walk the root recursively. For each ``isTree=true`` node, fetch
       the binary Tree from ``standardobjects/<shard>/<rest>``,
       decrypt it, parse it, and recurse over its children. For each
       file node, fetch each of its ``dataBlobLocs``, decrypt them,
       and concatenate to reconstruct the file content.

The reader uses :class:`arq_validator.backend.LocalBackend` so the
same path-traversal guard applies — relativePath fields in BlobLocs
that try to escape the backup root are rejected before any I/O.
"""

from __future__ import annotations

import os
import plistlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from arq_validator import constants as C
from arq_validator.backend import LocalBackend
from arq_validator.crypto import Keyset, decrypt_keyset
from arq_validator.layout import (
    discover_layout,
    find_latest_backuprecord,
)
from arq_writer.types import BlobLoc, FileNode, TreeNode

from .decrypt import DecryptError, decrypt_encrypted_object, decrypt_lz4_arqo
from .parse import parse_tree


ProgressCb = Callable[[str, dict], None]


@dataclass
class RestoreResult:
    src: Path
    dest: Path
    folder_uuid: str
    files_restored: int = 0
    dirs_restored: int = 0
    bytes_restored: int = 0
    blobs_fetched: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)


def _emit(cb: Optional[ProgressCb], kind: str, **payload: object) -> None:
    if cb is None:
        return
    try:
        cb(kind, dict(payload))
    except Exception:
        pass


class Restore:
    """Stateful restorer.

    One instance per (backup-source, password) pair. Reuse across
    multiple restore operations is safe — the keyset and discovered
    layout are cached internally on first call.
    """

    def __init__(
        self,
        src: Path,
        encryption_password: str,
        *,
        openssl_path: str = "openssl",
    ) -> None:
        self.src = Path(src).resolve()
        self.password = encryption_password
        self.openssl_path = openssl_path
        self.backend = LocalBackend(self.src)
        self._layouts = None
        self._keyset_by_computer: Dict[str, Keyset] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def layouts(self):
        if self._layouts is None:
            self._layouts = discover_layout(self.backend, "/")
        return self._layouts

    def list_folders(self) -> List[Tuple[str, str]]:
        """Return ``[(computer_uuid, folder_uuid)]`` pairs."""
        out: List[Tuple[str, str]] = []
        for lay in self.layouts():
            for f in lay.backup_folder_uuids:
                out.append((lay.computer_uuid, f))
        return out

    def keyset(self, computer_uuid: str) -> Keyset:
        if computer_uuid not in self._keyset_by_computer:
            kp = f"/{computer_uuid}/{C.KEYSET_FILE}"
            blob = self.backend.read_all(kp)
            self._keyset_by_computer[computer_uuid] = decrypt_keyset(
                blob, self.password, openssl_path=self.openssl_path,
            )
        return self._keyset_by_computer[computer_uuid]

    # ------------------------------------------------------------------
    # Single-restore helpers
    # ------------------------------------------------------------------

    def _fetch_blob(self, loc: BlobLoc, keyset: Keyset) -> bytes:
        """Fetch + decrypt + (LZ4-unwrap if needed) a referenced blob.

        Mirrors ``Arq7BlobReader.m::dataForBlobLoc:`` exactly:

        - Packed blob (``isPacked=true``): read
          ``backend.read_range(relativePath, offset, length)``.
        - Standalone blob: read the whole file at ``relativePath``.
        - If the bytes start with ``b"ARQO"``, decrypt; otherwise the
          backup is unencrypted (legal per spec) and the bytes are
          taken as-is.
        - If ``compressionType == 2`` (LZ4), decompress.
          ``compressionType == 0`` is "none". ``compressionType == 1``
          (Gzip) appears only in legacy Arq 5 data — supported here
          via stdlib ``gzip``.

        We do **not** need to know the pack file's header / index /
        framing — ``BlobLoc.offset`` and ``length`` are the only
        information required to slice out a blob.
        """
        if loc.isPacked:
            raw = self.backend.read_range(
                loc.relativePath, loc.offset, loc.length,
            )
        else:
            raw = self.backend.read_all(loc.relativePath)

        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, keyset.encryption_key, keyset.hmac_key,
                openssl_path=self.openssl_path,
            )

        if loc.compressionType == 2:
            from arq_writer.lz4_block import lz4_unwrap
            return lz4_unwrap(raw)
        if loc.compressionType == 1:
            import gzip
            return gzip.decompress(raw)
        if loc.compressionType == 0:
            return raw
        raise NotImplementedError(
            f"compressionType={loc.compressionType} not supported "
            f"(0=none, 1=Gzip, 2=LZ4 are implemented)"
        )

    @staticmethod
    def _blobloc_from_dict(d: Dict[str, Any]) -> BlobLoc:
        """Convert a plist-form BlobLoc dict to the dataclass form.

        Used when decoding the root Node dict embedded in a
        backuprecord plist.
        """
        return BlobLoc(
            blobIdentifier=d.get("blobIdentifier", "") or "",
            isPacked=bool(d.get("isPacked", False)),
            relativePath=d.get("relativePath", "") or "",
            offset=int(d.get("offset", 0)),
            length=int(d.get("length", 0)),
            stretchEncryptionKey=bool(d.get("stretchEncryptionKey", True)),
            compressionType=int(d.get("compressionType", 2)),
        )

    # ------------------------------------------------------------------
    # Walk + materialize
    # ------------------------------------------------------------------

    def _restore_dir_node(
        self,
        tree_blob_loc: BlobLoc,
        out_dir: Path,
        keyset: Keyset,
        result: RestoreResult,
        callback: Optional[ProgressCb],
    ) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        result.dirs_restored += 1
        try:
            tree_bytes = self._fetch_blob(tree_blob_loc, keyset)
            result.blobs_fetched += 1
        except (DecryptError, OSError, NotImplementedError) as exc:
            result.failures.append({
                "path": str(out_dir),
                "kind": "tree_fetch",
                "error": f"{type(exc).__name__}: {exc}",
            })
            _emit(callback, "tree_failed",
                  path=str(out_dir), error=str(exc))
            return
        tree = parse_tree(tree_bytes)
        _emit(callback, "tree_restored",
              path=str(out_dir), children=len(tree.children))
        for child in tree.children:
            child_out = out_dir / child.name
            if isinstance(child.node, TreeNode):
                self._restore_dir_node(
                    child.node.treeBlobLoc, child_out, keyset,
                    result, callback,
                )
            elif isinstance(child.node, FileNode):
                self._restore_file_node(
                    child.node, child_out, keyset, result, callback,
                )

    def _restore_file_node(
        self,
        node: FileNode,
        out_path: Path,
        keyset: Keyset,
        result: RestoreResult,
        callback: Optional[ProgressCb],
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        chunks: List[bytes] = []
        try:
            for loc in node.dataBlobLocs:
                chunks.append(self._fetch_blob(loc, keyset))
                result.blobs_fetched += 1
        except (DecryptError, OSError, NotImplementedError) as exc:
            result.failures.append({
                "path": str(out_path),
                "kind": "file_fetch",
                "error": f"{type(exc).__name__}: {exc}",
            })
            _emit(callback, "file_failed",
                  path=str(out_path), error=str(exc))
            return
        body = b"".join(chunks)
        out_path.write_bytes(body)
        result.bytes_restored += len(body)
        result.files_restored += 1
        # Best-effort metadata. mtime is the only field most file
        # systems can faithfully reproduce as a non-root user.
        try:
            if node.mtime_sec:
                ts = node.mtime_sec + node.mtime_nsec / 1_000_000_000
                os.utime(out_path, (ts, ts))
        except OSError:
            pass
        _emit(callback, "file_restored",
              path=str(out_path), size=len(body))

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def restore(
        self,
        *,
        folder_uuid: str,
        dest: Path,
        computer_uuid: Optional[str] = None,
        callback: Optional[ProgressCb] = None,
    ) -> RestoreResult:
        """Restore the most recent backuprecord of ``folder_uuid``.

        ``computer_uuid`` may be omitted when the destination has
        exactly one computer subtree (the common case for backups
        produced by our writer).
        """
        layouts = self.layouts()
        if computer_uuid is None:
            matches = [
                lay for lay in layouts
                if folder_uuid in lay.backup_folder_uuids
            ]
            if len(matches) == 0:
                raise ValueError(
                    f"folder UUID {folder_uuid!r} not found at {self.src}"
                )
            if len(matches) > 1:
                raise ValueError(
                    f"folder UUID {folder_uuid!r} ambiguous across "
                    f"{[m.computer_uuid for m in matches]}"
                )
            computer_uuid = matches[0].computer_uuid

        result = RestoreResult(
            src=self.src, dest=Path(dest).resolve(), folder_uuid=folder_uuid,
        )
        keyset = self.keyset(computer_uuid)

        record_path = find_latest_backuprecord(
            self.backend, "/", computer_uuid, folder_uuid,
        )
        if record_path is None:
            raise ValueError(
                f"no backuprecord found for {computer_uuid}/{folder_uuid}"
            )
        _emit(callback, "backuprecord_found",
              path=record_path,
              computer=computer_uuid, folder=folder_uuid)

        record_arqo = self.backend.read_all(record_path)
        record_plain = decrypt_lz4_arqo(
            record_arqo, keyset.encryption_key, keyset.hmac_key,
            openssl_path=self.openssl_path,
        )
        record = plistlib.loads(record_plain)
        node_dict = record.get("node")
        if not isinstance(node_dict, dict):
            raise ValueError("backuprecord missing or malformed `node` field")

        out_dir = Path(dest).resolve()
        if node_dict.get("isTree"):
            tree_blob_loc = self._blobloc_from_dict(
                node_dict["treeBlobLoc"]
            )
            self._restore_dir_node(
                tree_blob_loc, out_dir, keyset, result, callback,
            )
        else:
            # Root is a single file (rare but representable).
            file_node = FileNode(
                dataBlobLocs=[
                    self._blobloc_from_dict(b)
                    for b in node_dict.get("dataBlobLocs", [])
                ],
                itemSize=int(node_dict.get("itemSize", 0)),
                mtime_sec=int(node_dict.get("modificationTime_sec", 0)),
                mtime_nsec=int(node_dict.get("modificationTime_nsec", 0)),
            )
            self._restore_file_node(
                file_node, out_dir, keyset, result, callback,
            )
        _emit(callback, "restore_finished",
              files=result.files_restored,
              dirs=result.dirs_restored,
              bytes=result.bytes_restored,
              failures=len(result.failures))
        return result
