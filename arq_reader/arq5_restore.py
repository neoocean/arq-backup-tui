"""High-level Arq 5 / Arq 6 restorer.

Glues together the existing pieces — the Arq 5 keyset decryption, the
binary parsers, the ``.pack``/``.index`` reader, and the ARQO
decryptor — into a single ``Arq5Restore`` walker analogous to the v0
:class:`arq_reader.restore.Restore` for Arq 7.

Workflow per backup folder:

    1. Discover computer UUIDs by looking for ``encryptionvN.dat`` at
       the top level.
    2. Decrypt the keyset (v2 or v3 — see :mod:`arq5_keyset`).
    3. List backup folders under ``bucketdata/`` for each computer.
    4. Read ``bucketdata/<folder>/refs/heads/master`` — strip the
       trailing ``Y`` to get the head commit's SHA-1.
    5. Resolve the commit blob (try ``objects/`` paths, then
       ``packsets/<folder>-trees/<sha>.{pack,index}`` lookup).
    6. Decrypt the ARQO + LZ4-decompress (LZ4 if compressionType=2;
       Gzip if 1; raw if 0). Parse with
       :func:`arq_reader.arq5_binary.parse_commit`.
    7. Follow ``treeBlobKey`` → fetch + decrypt + decompress + parse
       as Tree. Recurse over its child Nodes:
         - directory Node: fetch the child Tree blob via the Node's
           data SHA-1 (the tree blob is stored under that SHA-1)
         - file Node: concatenate every ``dataBlobKey`` plaintext
           and write the result to disk

Arq 5/6 stores tree compression metadata both at the Tree level
(applied to xattrs/acl blobs) and at each Node level (applied to its
own data + xattrs + acl blobs). Each ``BlobKey`` carries the
inherited ``compressionType`` so the restorer doesn't have to track
it externally.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from arq_validator.backend import LocalBackend

from .arq5_binary import (
    Arq5Commit,
    Arq5Node,
    Arq5Tree,
    BlobKey,
    parse_commit,
    parse_tree,
)
from .arq5_keyset import (
    Arq5Keyset,
    arq5_object_paths,
    decrypt_arq5_keyset,
)
from .arq5_pack import PackIndexEntry, parse_pack_index
from .decrypt import decrypt_encrypted_object


ProgressCb = Callable[[str, dict], None]


@dataclass
class Arq5RestoreResult:
    src: Path
    dest: Path
    computer_uuid: str
    folder_uuid: str
    commit_sha1: str = ""
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


def _decompress(plaintext: bytes, compression: int) -> bytes:
    """Apply the compressionType inverse: 0=none, 1=Gzip, 2=LZ4."""
    if compression == 0:
        return plaintext
    if compression == 1:
        return gzip.decompress(plaintext)
    if compression == 2:
        from arq_writer.lz4_block import lz4_unwrap
        return lz4_unwrap(plaintext)
    raise ValueError(f"unsupported compressionType: {compression}")


class Arq5Restore:
    """One-shot restore against an Arq 5/6 backup destination.

    Construction does no I/O. ``list_computers`` /
    ``list_folders`` / ``restore`` are the entry points; intermediate
    computation (keyset decrypt, pack-index loading) is cached on the
    instance so multiple restores against the same destination don't
    pay repeated cost.
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
        self._keyset_cache: Dict[str, Arq5Keyset] = {}
        # path-to-.index → list of entries.
        self._pack_index_cache: Dict[str, List[PackIndexEntry]] = {}
        # sha1_hex -> (pack_path, offset, length) global lookup. Built
        # lazily by _scan_packs() the first time a SHA-1 isn't found
        # under objects/.
        self._packed_sha1: Dict[str, tuple] = {}
        self._packs_scanned: bool = False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_computers(self) -> List[str]:
        """Return computer UUIDs that have an ``encryptionvN.dat``."""
        out: List[str] = []
        try:
            entries = self.backend.list_dir("/")
        except OSError:
            return out
        for name in entries:
            if not self.backend.is_dir(f"/{name}"):
                continue
            for v in (3, 2, 1):
                if self.backend.exists(f"/{name}/encryptionv{v}.dat"):
                    out.append(name)
                    break
        return sorted(out)

    def list_folders(self, computer_uuid: str) -> List[str]:
        """Return backup folder UUIDs under
        ``<computer-uuid>/bucketdata/``.
        """
        bucket = f"/{computer_uuid}/bucketdata"
        if not self.backend.is_dir(bucket):
            return []
        try:
            entries = self.backend.list_dir(bucket)
        except OSError:
            return []
        return sorted(
            e for e in entries if self.backend.is_dir(f"{bucket}/{e}")
        )

    def get_keyset(self, computer_uuid: str) -> Arq5Keyset:
        if computer_uuid not in self._keyset_cache:
            blob = None
            for v in (3, 2):
                p = f"/{computer_uuid}/encryptionv{v}.dat"
                if self.backend.exists(p):
                    blob = self.backend.read_all(p)
                    break
            if blob is None:
                raise FileNotFoundError(
                    f"no encryptionv2/v3.dat under {computer_uuid}"
                )
            self._keyset_cache[computer_uuid] = decrypt_arq5_keyset(
                blob, self.password, computer_uuid,
                openssl_path=self.openssl_path,
            )
        return self._keyset_cache[computer_uuid]

    # ------------------------------------------------------------------
    # Object lookup
    # ------------------------------------------------------------------

    def _find_in_packsets(
        self, computer_uuid: str, sha1: str,
    ) -> Optional[tuple]:
        """Search packsets for a SHA-1, returning
        ``(pack_path, offset, length)`` if present.

        Pack indexes are loaded lazily on first miss; subsequent
        lookups use the cache. ``packsets/`` may not exist for
        destinations that store everything under ``objects/``.
        """
        if not self._packs_scanned:
            self._scan_packs(computer_uuid)
        return self._packed_sha1.get(sha1)

    def _scan_packs(self, computer_uuid: str) -> None:
        self._packs_scanned = True
        ps_root = f"/{computer_uuid}/packsets"
        if not self.backend.is_dir(ps_root):
            return
        try:
            packsets = self.backend.list_dir(ps_root)
        except OSError:
            return
        for ps in packsets:
            ps_dir = f"{ps_root}/{ps}"
            if not self.backend.is_dir(ps_dir):
                continue
            try:
                files = self.backend.list_dir(ps_dir)
            except OSError:
                continue
            for f in files:
                if not f.endswith(".index"):
                    continue
                idx_path = f"{ps_dir}/{f}"
                pack_path = idx_path[: -len(".index")] + ".pack"
                try:
                    idx_bytes = self.backend.read_all(idx_path)
                except OSError:
                    continue
                try:
                    entries = parse_pack_index(idx_bytes)
                except Exception:
                    continue
                self._pack_index_cache[idx_path] = entries
                for e in entries:
                    self._packed_sha1[e.sha1_hex] = (
                        pack_path, e.offset, e.data_length,
                    )

    def fetch_object_bytes(
        self, computer_uuid: str, sha1: str,
    ) -> bytes:
        """Locate + return the raw on-disk bytes for ``sha1``.

        Tries every known ``objects/`` layout, then packsets. Raises
        :class:`FileNotFoundError` if no layout finds the blob.
        """
        for path in arq5_object_paths(computer_uuid, sha1):
            if self.backend.exists(path):
                return self.backend.read_all(path)
        located = self._find_in_packsets(computer_uuid, sha1)
        if located is None:
            raise FileNotFoundError(
                f"blob {sha1} not found under "
                f"{computer_uuid}/objects/* or packsets/*"
            )
        pack_path, offset, length = located
        # Arq 5 .pack entries have a 10-byte per-entry header before
        # the data: [String:mime null][String:name null][UInt64:length]
        # = 1 + 1 + 8 = 10 bytes. The .index offset points at the
        # START of the entry, so we read the full entry slice and
        # strip the 10-byte header.
        slice_bytes = self.backend.read_range(
            pack_path, offset, 1 + 1 + 8 + length,
        )
        return slice_bytes[10 : 10 + length]

    # ------------------------------------------------------------------
    # Decrypt + decompress + parse
    # ------------------------------------------------------------------

    def fetch_blob(
        self, computer_uuid: str, blob_key: BlobKey, keyset: Arq5Keyset,
    ) -> bytes:
        """Fetch a blob referenced by ``blob_key`` and return its
        plaintext (post-decrypt, post-decompress).
        """
        raw = self.fetch_object_bytes(computer_uuid, blob_key.sha1)
        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, keyset.encryption_key, keyset.hmac_key,
                openssl_path=self.openssl_path,
            )
        return _decompress(raw, blob_key.compressionType)

    def fetch_blob_by_sha1(
        self, computer_uuid: str, sha1: str, keyset: Arq5Keyset,
        *, compression_type: int = 0,
    ) -> bytes:
        """Lower-level fetch for cases where we have a bare SHA-1
        (commit ref).
        """
        raw = self.fetch_object_bytes(computer_uuid, sha1)
        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, keyset.encryption_key, keyset.hmac_key,
                openssl_path=self.openssl_path,
            )
        return _decompress(raw, compression_type)

    # ------------------------------------------------------------------
    # Top-level walk
    # ------------------------------------------------------------------

    def get_master_commit_sha(
        self, computer_uuid: str, folder_uuid: str,
    ) -> str:
        """Read ``bucketdata/<folder>/refs/heads/master`` and strip
        the trailing ``Y``.
        """
        path = (
            f"/{computer_uuid}/bucketdata/{folder_uuid}/refs/heads/master"
        )
        body = self.backend.read_all(path)
        text = body.decode("ascii", errors="replace").strip()
        if text.endswith("Y"):
            text = text[:-1]
        if not text:
            raise ValueError(
                f"empty master ref at {path}"
            )
        return text

    def restore(
        self,
        *,
        computer_uuid: str,
        folder_uuid: str,
        dest: Path,
        callback: Optional[ProgressCb] = None,
    ) -> Arq5RestoreResult:
        """Restore the most recent commit of ``folder_uuid`` to ``dest``.

        Walks the tree depth-first, materializing directories and
        files as it goes. Failures (missing blob, bad HMAC, malformed
        Tree) land in ``result.failures`` and don't abort the run.
        """
        dest = Path(dest).resolve()
        result = Arq5RestoreResult(
            src=self.src, dest=dest,
            computer_uuid=computer_uuid, folder_uuid=folder_uuid,
        )
        keyset = self.get_keyset(computer_uuid)
        commit_sha = self.get_master_commit_sha(computer_uuid, folder_uuid)
        result.commit_sha1 = commit_sha
        _emit(callback, "commit_found",
              computer=computer_uuid, folder=folder_uuid, sha1=commit_sha)

        # Commits stored pre-LZ4-spec are uncompressed; v10+ Commits
        # may be LZ4. We don't know without parsing — try uncompressed
        # first; if header doesn't match "CommitV", retry as LZ4.
        commit_plain = self.fetch_blob_by_sha1(
            computer_uuid, commit_sha, keyset, compression_type=0,
        )
        if not commit_plain.startswith(b"CommitV"):
            commit_plain = self.fetch_blob_by_sha1(
                computer_uuid, commit_sha, keyset, compression_type=2,
            )
        commit = parse_commit(commit_plain)
        result.blobs_fetched += 1

        if commit.treeBlobKey is None:
            raise ValueError(
                f"commit {commit_sha} has no tree blob key"
            )

        tree_plain = self.fetch_blob(
            computer_uuid, commit.treeBlobKey, keyset,
        )
        result.blobs_fetched += 1
        tree = parse_tree(tree_plain)
        _emit(callback, "tree_root_loaded",
              computer=computer_uuid, version=tree.version,
              children=len(tree.nodes))

        dest.mkdir(parents=True, exist_ok=True)
        result.dirs_restored += 1
        self._walk_tree(tree, dest, computer_uuid, keyset, result, callback)
        _emit(callback, "restore_finished",
              files=result.files_restored, dirs=result.dirs_restored,
              failures=len(result.failures))
        return result

    # ------------------------------------------------------------------
    # Tree walker (internal)
    # ------------------------------------------------------------------

    def _walk_tree(
        self,
        tree: Arq5Tree,
        out_dir: Path,
        computer_uuid: str,
        keyset: Arq5Keyset,
        result: Arq5RestoreResult,
        callback: Optional[ProgressCb],
    ) -> None:
        for name, node in tree.nodes.items():
            child_out = out_dir / name
            if node.isTree:
                self._restore_dir_node(
                    name, node, child_out, computer_uuid, keyset,
                    result, callback,
                )
            else:
                self._restore_file_node(
                    name, node, child_out, computer_uuid, keyset,
                    result, callback,
                )

    def _restore_dir_node(
        self,
        name: str,
        node: Arq5Node,
        out_dir: Path,
        computer_uuid: str,
        keyset: Arq5Keyset,
        result: Arq5RestoreResult,
        callback: Optional[ProgressCb],
    ) -> None:
        """For a tree-typed Node, the tree blob is referenced by the
        Node's first (and only) ``dataBlobKey`` — same as Arq 5/6
        commits' ``treeBlobKey``. Fetch it, parse, recurse.
        """
        if not node.dataBlobKeys:
            result.failures.append({
                "path": str(out_dir),
                "kind": "tree_blobkey_missing",
                "error": f"directory node has no dataBlobKey",
            })
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        result.dirs_restored += 1
        try:
            tree_plain = self.fetch_blob(
                computer_uuid, node.dataBlobKeys[0], keyset,
            )
            result.blobs_fetched += 1
            sub_tree = parse_tree(tree_plain)
        except Exception as exc:
            result.failures.append({
                "path": str(out_dir),
                "kind": "tree_fetch",
                "error": f"{type(exc).__name__}: {exc}",
            })
            _emit(callback, "tree_failed",
                  path=str(out_dir), error=str(exc))
            return
        _emit(callback, "tree_restored",
              path=str(out_dir), children=len(sub_tree.nodes))
        self._walk_tree(
            sub_tree, out_dir, computer_uuid, keyset, result, callback,
        )

    def _restore_file_node(
        self,
        name: str,
        node: Arq5Node,
        out_path: Path,
        computer_uuid: str,
        keyset: Arq5Keyset,
        result: Arq5RestoreResult,
        callback: Optional[ProgressCb],
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        chunks: List[bytes] = []
        try:
            for bk in node.dataBlobKeys:
                chunks.append(self.fetch_blob(
                    computer_uuid, bk, keyset,
                ))
                result.blobs_fetched += 1
        except Exception as exc:
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
        _emit(callback, "file_restored",
              path=str(out_path), size=len(body))
