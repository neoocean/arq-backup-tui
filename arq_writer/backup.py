"""High-level backup orchestrator.

Walks a source directory tree, encrypts each file's contents into a
single standalone ``EncryptedObject`` under
``standardobjects/<2-hex-shard>/<62-hex-blobid>``, builds a ``Tree`` per
directory (also encrypted into ``standardobjects/``), and writes a
single ``backuprecord`` describing the run.

Output structure (under ``dest_root``):

    <computer-uuid>/
        encryptedkeyset.dat
        backupconfig.json
        backupfolders.json
        backupplan.json
        backupfolders/<folder-uuid>/
            backupfolder.json
            backuprecords/00001/<creation-date>.backuprecord
        standardobjects/
            <2-hex-shard>/<62-hex-blobid>     # one per blob

This is the v0 writer that intentionally bypasses two genuinely
under-documented Arq 7 internals (the ``treepacks/`` / ``blobpacks/``
container layout, and the chunker's ``chunkerVersion: 3 + useBuzhash``
parameters). The trade-off is more files on disk and weaker dedup of
modified-in-place files; byte-identical files still dedup via SHA-256
blob ID. ``arq_restore`` (BSD reference reader) and Arq.app both parse
this layout correctly because the spec explicitly permits standalone
``standardobjects/`` storage with ``BlobLoc.isPacked = false``.
"""

from __future__ import annotations

import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .backuprecord import (
    build_backuprecord_arqo,
    build_backuprecord_dict,
)
from .constants import (
    BACKUPFOLDERS_DIR,
    BACKUPRECORDS_DIR,
    BLOBPACKS_DIR,
    COMPRESSION_LZ4,
    KEYSET_FILE,
    KEYSET_PLAIN_FIELD_LEN,
    STANDARDOBJECTS_DIR,
    TREEPACKS_DIR,
    TREE_VERSION,
)
from .crypto_write import (
    build_encrypted_keyset,
    build_encrypted_object,
    compute_blob_id,
)
from .json_configs import (
    build_backupconfig,
    build_backupfolder_json,
    build_backupfolders_json,
    build_backupplan,
    build_folder_plan,
)
from .chunker import Buzhash, ChunkerConfig
from .lz4_block import lz4_wrap
from .pack_builder import DEFAULT_MAX_PACK_BYTES, PackBuilder
from .serialize import write_tree
from .types import BlobLoc, FileNode, Node, Tree, TreeChild, TreeNode


ProgressCb = Callable[[str, dict], None]


@dataclass
class BackupResult:
    """Outcome of a single ``build_backup`` run."""

    dest_root: Path
    computer_uuid: str
    plan_uuid: str
    folder_uuid: str
    backuprecord_path: Path
    files_written: int = 0
    trees_written: int = 0
    bytes_plaintext: int = 0
    bytes_on_disk: int = 0
    blob_ids: List[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed_sec(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


def _emit(cb: Optional[ProgressCb], kind: str, **payload: object) -> None:
    if cb is None:
        return
    try:
        cb(kind, dict(payload))
    except Exception:
        pass


def _shard_for(blob_id: str) -> Tuple[str, str]:
    """Split a 64-char SHA-256 hex into (shard, name).

    Arq 7 names standalone objects as ``<shard>/<rest>`` where
    ``shard = blob_id[:2]`` and ``rest = blob_id[2:]`` (62 chars).
    """
    return blob_id[:2], blob_id[2:]


def _path_for_blob(computer_uuid: str, blob_id: str) -> str:
    """Absolute on-disk path under the computer's standardobjects/."""
    shard, rest = _shard_for(blob_id)
    return f"/{computer_uuid}/{STANDARDOBJECTS_DIR}/{shard}/{rest}"


def _absolute(dest_root: Path, rel: str) -> Path:
    """Resolve a relative-to-computer path under dest_root."""
    return dest_root / rel.lstrip("/")


def _try_load_existing_keyset(
    dest_root: Path, computer_uuid: str, password: str,
    *, backend=None,
) -> Optional[Tuple[bytes, bytes, bytes]]:
    """Best-effort: read an existing ``encryptedkeyset.dat`` and
    return ``(encryption_key, hmac_key, blob_id_salt)`` so a
    follow-up backup against the same destination produces matching
    blob_ids.

    Returns ``None`` on any error (file missing, wrong password,
    corrupt blob, network failure). The caller falls back to fresh
    random keys — correct, but defeats cross-run dedup.
    """
    keyset_rel = f"/{computer_uuid}/{KEYSET_FILE}"
    try:
        if backend is not None:
            if not backend.exists(keyset_rel):
                return None
            blob = backend.read_all(keyset_rel)
        else:
            keyset_path = Path(dest_root) / computer_uuid / KEYSET_FILE
            if not keyset_path.is_file():
                return None
            blob = keyset_path.read_bytes()
        from arq_validator.crypto import decrypt_keyset
        ks = decrypt_keyset(blob, password)
    except Exception:
        return None
    return ks.encryption_key, ks.hmac_key, ks.blob_id_salt


def _build_file_blobloc(
    computer_uuid: str, blob_id: str, length_on_disk: int,
) -> BlobLoc:
    return BlobLoc(
        blobIdentifier=blob_id,
        isPacked=False,
        relativePath=_path_for_blob(computer_uuid, blob_id),
        offset=0,
        length=length_on_disk,
        stretchEncryptionKey=True,
        compressionType=COMPRESSION_LZ4,
    )


class Backup:
    """Stateful backup writer.

    The class is the building block; most callers use the
    :func:`build_backup` convenience wrapper instead. Keep an instance
    around if you need to perform a multi-folder backup (call
    ``init_plan`` once, then ``add_folder`` per source).
    """

    def __init__(
        self,
        dest_root: Path,
        encryption_password: str,
        *,
        backup_name: str = "TUI backup",
        computer_name: Optional[str] = None,
        plan_name: Optional[str] = None,
        computer_uuid: Optional[str] = None,
        plan_uuid: Optional[str] = None,
        encryption_key: Optional[bytes] = None,
        hmac_key: Optional[bytes] = None,
        blob_id_salt: Optional[bytes] = None,
        openssl_path: str = "openssl",
        use_packs: bool = False,
        max_pack_bytes: int = DEFAULT_MAX_PACK_BYTES,
        chunker_config: Optional[ChunkerConfig] = None,
        dedup_against_existing: bool = False,
        backend=None,
        callback: Optional[ProgressCb] = None,
    ) -> None:
        # When ``backend`` is None we drive a LocalBackend rooted at
        # dest_root (so all backend-relative paths starting with /
        # land under dest_root on disk). When ``backend`` is given,
        # paths are passed through verbatim and ``dest_root`` is
        # only used internally for path *construction*; for SFTP
        # callers should pass dest_root="/" (or the SFTP-server-side
        # root that anchors the backup destination).
        if backend is None:
            from arq_validator.backend import LocalBackend
            self.dest_root = Path(dest_root).resolve()
            # The writer creates the destination on demand; LocalBackend
            # requires the root to exist, so materialize it here.
            self.dest_root.mkdir(parents=True, exist_ok=True)
            self.backend = LocalBackend(self.dest_root)
        else:
            # dest_root is a path *within* the backend's namespace.
            # Keep it as-is (don't resolve, which is local-fs-only).
            self.dest_root = Path(dest_root)
            self.backend = backend
        self.password = encryption_password
        self.backup_name = backup_name
        self.computer_name = computer_name or os.uname().nodename
        self.plan_name = plan_name or backup_name
        self.computer_uuid = (
            computer_uuid or str(uuid.uuid4()).upper()
        )
        self.plan_uuid = plan_uuid or str(uuid.uuid4()).upper()
        # When dedup_against_existing is on, try to reuse the
        # destination's existing keyset so blob_ids (which are
        # SHA-256 over salt+plaintext) line up across runs. Fall
        # through to fresh random keys if anything is missing.
        loaded_keys = None
        if (
            dedup_against_existing
            and encryption_key is None
            and hmac_key is None
            and blob_id_salt is None
        ):
            loaded_keys = _try_load_existing_keyset(
                self.dest_root,
                self.computer_uuid,
                encryption_password,
                backend=self.backend,
            )
        if loaded_keys is not None:
            self.encryption_key, self.hmac_key, self.blob_id_salt = (
                loaded_keys
            )
            self._keyset_was_reused = True
        else:
            self.encryption_key = (
                encryption_key or secrets.token_bytes(KEYSET_PLAIN_FIELD_LEN)
            )
            self.hmac_key = (
                hmac_key or secrets.token_bytes(KEYSET_PLAIN_FIELD_LEN)
            )
            self.blob_id_salt = (
                blob_id_salt or secrets.token_bytes(KEYSET_PLAIN_FIELD_LEN)
            )
            self._keyset_was_reused = False
        self.openssl_path = openssl_path
        self.use_packs = use_packs
        self.max_pack_bytes = max_pack_bytes
        self.chunker_config = chunker_config
        self._chunker: Optional[Buzhash] = (
            Buzhash(chunker_config) if chunker_config is not None else None
        )
        self.dedup_against_existing = dedup_against_existing
        self.callback = callback

        # Per-run accumulators.
        self.files_written = 0
        self.files_reused = 0          # tree-walk reuse counter
        self.trees_written = 0
        self.bytes_plaintext = 0
        self.bytes_on_disk = 0
        self.blob_ids: List[str] = []
        # Folder plans accumulated across add_folder() calls so the
        # backuprecord embeds an up-to-date plan snapshot.
        self._folder_plans: List[dict] = []

        # On-disk dedup cache: avoid rewriting an existing standalone
        # object when an identical-content blob has already been
        # written in the same run. Maps blob_id -> BlobLoc so packed
        # and standalone modes share a single source of truth.
        self._written_blobs: Dict[str, BlobLoc] = {}

        # Per-family pack builders, lazily initialized on first use
        # to avoid creating empty pack-shaped paths when use_packs is
        # disabled.
        self._blob_pack: Optional[PackBuilder] = None
        self._tree_pack: Optional[PackBuilder] = None

        # Prior-tree index for tree-walk reuse (cross-run skip of
        # read+chunk on unchanged files). Populated lazily in
        # add_folder() per folder_uuid; None means "no prior backup
        # for this folder, walk normally".
        self._prior_tree = None

    # ------------------------------------------------------------------
    # Plan-level setup
    # ------------------------------------------------------------------

    def computer_root(self) -> Path:
        return self.dest_root / self.computer_uuid

    def _cu_path(self, *parts: str) -> str:
        """Return a backend-relative POSIX path under the computer
        root: ``"/<cu>/<parts...>"``."""
        return "/" + "/".join((self.computer_uuid, *parts))

    def init_plan(self) -> None:
        """Write the four root-level configs + ``encryptedkeyset.dat``.

        Idempotent — calling twice rewrites the files with the same
        content (modulo the random IV/salt in the keyset, which is
        regenerated on each call).
        """
        self.backend.mkdir(self._cu_path(), parents=True, exist_ok=True)
        self.backend.mkdir(
            self._cu_path(STANDARDOBJECTS_DIR), exist_ok=True,
        )
        self.backend.mkdir(
            self._cu_path(BACKUPFOLDERS_DIR), exist_ok=True,
        )

        # Don't overwrite an existing valid keyset — the prior
        # backuprecords were encrypted under that one's master key,
        # so rewriting it (with a fresh random IV/salt) would break
        # restore of every record older than this run.
        keyset_path = self._cu_path(KEYSET_FILE)
        if not self._keyset_was_reused:
            keyset_blob = build_encrypted_keyset(
                self.password,
                self.encryption_key, self.hmac_key, self.blob_id_salt,
                openssl_path=self.openssl_path,
            )
            self.backend.write_all(keyset_path, keyset_blob)
            _emit(
                self.callback, "keyset_written",
                path=keyset_path, size=len(keyset_blob),
            )
        else:
            _emit(
                self.callback, "keyset_reused", path=keyset_path,
            )

        config = build_backupconfig(
            backup_name=self.backup_name,
            computer_name=self.computer_name,
            is_encrypted=True,
        )
        self.backend.write_all(
            self._cu_path("backupconfig.json"),
            json.dumps(config, indent=2).encode("utf-8"),
        )

        folders_idx = build_backupfolders_json(self.computer_uuid)
        self.backend.write_all(
            self._cu_path("backupfolders.json"),
            json.dumps(folders_idx, indent=2).encode("utf-8"),
        )

        # Plan written here is a placeholder; rewritten on each
        # add_folder() call so it always reflects the current state.
        self._write_plan_json()

        # Seed the within-run dedup cache from any prior backups
        # already on this destination so we don't re-encrypt + re-
        # write identical content. Best-effort: parse failures don't
        # abort the run, they just lose the dedup boost.
        if self.dedup_against_existing:
            from .dedup import seed_existing_destination
            report = seed_existing_destination(
                self.dest_root, self.computer_uuid,
                self._written_blobs,
                encryption_key=self.encryption_key,
                hmac_key=self.hmac_key,
                backend=self.backend,
            )
            _emit(self.callback, "dedup_seeded", **report)

        _emit(self.callback, "plan_initialized",
              computer_uuid=self.computer_uuid,
              plan_uuid=self.plan_uuid)

    def _write_plan_json(self) -> None:
        plan = build_backupplan(
            plan_uuid=self.plan_uuid,
            plan_name=self.plan_name,
            folder_plans=self._folder_plans,
            is_encrypted=True,
            creation_time=time.time(),
            update_time=time.time(),
        )
        self.backend.write_all(
            self._cu_path("backupplan.json"),
            json.dumps(plan, indent=2).encode("utf-8"),
        )

    # ------------------------------------------------------------------
    # Blob writing
    # ------------------------------------------------------------------

    def _write_blob(self, plaintext: bytes, *, is_tree: bool = False) -> BlobLoc:
        """Encrypt+LZ4-wrap+write ``plaintext`` and return its BlobLoc.

        Routing depends on ``self.use_packs``:

        - **standalone mode** (``use_packs=False``, the v0 default):
          one file per blob under ``standardobjects/<shard>/<rest>``.
        - **packed mode** (``use_packs=True``): blobs accumulate into
          ``treepacks/`` (when ``is_tree``) or ``blobpacks/`` and
          flush when the buffer crosses ``max_pack_bytes``.

        Identical-content blobs (same SHA-256 ``blob_id``) reuse the
        first call's BlobLoc — including offset/length when in packed
        mode, so pack files don't carry duplicate ARQOs.
        """
        blob_id = compute_blob_id(self.blob_id_salt, plaintext)
        cached = self._written_blobs.get(blob_id)
        if cached is not None:
            return cached
        lz4_bytes = lz4_wrap(plaintext)
        arqo = build_encrypted_object(
            lz4_bytes, self.encryption_key, self.hmac_key,
            openssl_path=self.openssl_path,
        )

        if self.use_packs:
            if is_tree:
                if self._tree_pack is None:
                    self._tree_pack = PackBuilder(
                        self.computer_uuid, TREEPACKS_DIR,
                        self.dest_root,
                        max_pack_bytes=self.max_pack_bytes,
                        compression_type=COMPRESSION_LZ4,
                        backend=self.backend,
                    )
                loc = self._tree_pack.add(blob_id, arqo)
            else:
                if self._blob_pack is None:
                    self._blob_pack = PackBuilder(
                        self.computer_uuid, BLOBPACKS_DIR,
                        self.dest_root,
                        max_pack_bytes=self.max_pack_bytes,
                        compression_type=COMPRESSION_LZ4,
                        backend=self.backend,
                    )
                loc = self._blob_pack.add(blob_id, arqo)
        else:
            blob_rel = _path_for_blob(self.computer_uuid, blob_id)
            parent = blob_rel.rsplit("/", 1)[0] or "/"
            self.backend.mkdir(parent, parents=True, exist_ok=True)
            self.backend.write_all(blob_rel, arqo)
            loc = _build_file_blobloc(
                self.computer_uuid, blob_id, len(arqo),
            )

        self._written_blobs[blob_id] = loc
        self.bytes_plaintext += len(plaintext)
        self.bytes_on_disk += len(arqo)
        self.blob_ids.append(blob_id)
        return loc

    def flush_packs(self) -> None:
        """Flush all in-flight pack builders to disk.

        Called automatically at the end of each ``add_folder`` so the
        BlobLocs the backuprecord embeds reference fully-materialized
        pack files. Safe to call multiple times — empty buffers no-op.
        """
        if self._blob_pack is not None:
            self._blob_pack.close()
        if self._tree_pack is not None:
            self._tree_pack.close()

    # ------------------------------------------------------------------
    # Folder walk
    # ------------------------------------------------------------------

    def _walk(self, source: Path, rel_path: str = "") -> Tuple[Node, int, int]:
        """Recursively encode ``source`` into a Node + write blobs.

        ``rel_path`` is the source-root-relative POSIX path of
        ``source`` (empty string for the root). It's used by the
        prior-tree index to look up previously-recorded FileNodes
        and skip re-reading unchanged content.

        Returns ``(node, item_size, contained_files_count)``.
        Symlinks are NOT followed (each is treated as a regular file
        whose content is the link target — pragmatic, matches Arq's
        own conservatism).
        """
        if source.is_dir() and not source.is_symlink():
            return self._walk_dir(source, rel_path=rel_path)
        return self._walk_file(source, rel_path=rel_path)

    def _walk_file(
        self, src: Path, rel_path: str = "",
    ) -> Tuple[FileNode, int, int]:
        # Tree-walk reuse: if we have a prior tree index AND the
        # prior FileNode at this rel_path matches the source's
        # current (mtime, size, mode), skip the read + chunk + hash
        # path entirely and reuse the prior dataBlobLocs.
        if self._prior_tree is not None and rel_path:
            try:
                stat_now = src.stat()
            except OSError:
                stat_now = None
            if stat_now is not None:
                prior = self._prior_tree.stat_matches(rel_path, stat_now)
                if prior is not None:
                    # Tally each reused BlobLoc as a "seen" blob_id
                    # so subsequent identical-content writes hit the
                    # within-run cache too.
                    for loc in prior.dataBlobLocs:
                        self._written_blobs.setdefault(
                            loc.blobIdentifier, loc,
                        )
                    from .prior_tree import reuse_file_node_for
                    node = reuse_file_node_for(stat_now, prior)
                    self.files_written += 1
                    self.files_reused += 1
                    _emit(self.callback, "file_reused",
                          path=str(src),
                          rel_path=rel_path,
                          size=int(stat_now.st_size),
                          chunks=len(node.dataBlobLocs))
                    return node, int(stat_now.st_size), 1
        try:
            data = src.read_bytes()
        except OSError as exc:
            _emit(self.callback, "file_read_error",
                  path=str(src), error=str(exc))
            data = b""
        # When the Buzhash chunker is enabled, files larger than the
        # min-chunk threshold get split into multiple dataBlobLocs;
        # restore concatenates them back in order. Empty / tiny files
        # still produce a single blob (chunk yields the full input).
        if self._chunker is not None and data:
            locs = [
                self._write_blob(piece) for piece in self._chunker.chunk(data)
            ]
        else:
            locs = [self._write_blob(data)]
        st = src.stat()
        node = FileNode(
            dataBlobLocs=locs,
            itemSize=len(data),
            containedFilesCount=1,
            mtime_sec=int(st.st_mtime),
            mtime_nsec=int((st.st_mtime - int(st.st_mtime)) * 1_000_000_000),
            ctime_sec=int(st.st_ctime),
            ctime_nsec=int((st.st_ctime - int(st.st_ctime)) * 1_000_000_000),
            mac_st_mode=st.st_mode,
            mac_st_uid=st.st_uid if hasattr(st, "st_uid") else 0,
            mac_st_gid=st.st_gid if hasattr(st, "st_gid") else 0,
            mac_st_ino=st.st_ino,
            mac_st_nlink=st.st_nlink,
        )
        self.files_written += 1
        _emit(self.callback, "file_written",
              path=str(src), size=len(data),
              chunks=len(locs),
              blob_id=locs[0].blobIdentifier if locs else "")
        return node, len(data), 1

    def _walk_dir(
        self, src: Path, rel_path: str = "",
    ) -> Tuple[TreeNode, int, int]:
        children: List[TreeChild] = []
        item_size = 0
        contained = 0
        try:
            entries = sorted(src.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            _emit(self.callback, "dir_read_error",
                  path=str(src), error=str(exc))
            entries = []
        for entry in entries:
            child_rel = (
                f"{rel_path}/{entry.name}" if rel_path else entry.name
            )
            child_node, child_size, child_count = self._walk(
                entry, rel_path=child_rel,
            )
            children.append(TreeChild(name=entry.name, node=child_node))
            item_size += child_size
            contained += child_count

        tree = Tree(children=children, version=TREE_VERSION)
        tree_bytes = write_tree(tree, version=TREE_VERSION)
        tree_loc = self._write_blob(tree_bytes, is_tree=True)
        self.trees_written += 1

        st = src.stat()
        node = TreeNode(
            treeBlobLoc=tree_loc,
            itemSize=item_size,
            containedFilesCount=contained,
            mtime_sec=int(st.st_mtime),
            mtime_nsec=int((st.st_mtime - int(st.st_mtime)) * 1_000_000_000),
            ctime_sec=int(st.st_ctime),
            ctime_nsec=int((st.st_ctime - int(st.st_ctime)) * 1_000_000_000),
            mac_st_mode=st.st_mode,
            mac_st_uid=st.st_uid if hasattr(st, "st_uid") else 0,
            mac_st_gid=st.st_gid if hasattr(st, "st_gid") else 0,
            mac_st_ino=st.st_ino,
        )
        _emit(self.callback, "tree_written",
              path=str(src), children=len(children),
              blob_id=tree_loc.blobIdentifier)
        return node, item_size, contained

    # ------------------------------------------------------------------
    # Folder-level operations
    # ------------------------------------------------------------------

    def add_folder(
        self,
        source: Path,
        *,
        folder_uuid: Optional[str] = None,
        folder_name: Optional[str] = None,
        local_mount_point: str = "/",
    ) -> Path:
        """Walk ``source`` and write the resulting backuprecord.

        Returns the absolute path to the written backuprecord file.
        Multiple calls accumulate into the same plan; ``backupplan.json``
        is rewritten each time so it stays in sync.
        """
        source = Path(source).resolve()
        folder_uuid = folder_uuid or str(uuid.uuid4()).upper()
        folder_name = folder_name or source.name or "root"

        # Materialize the folder dirs.
        bf_rel = self._cu_path(BACKUPFOLDERS_DIR, folder_uuid)
        self.backend.mkdir(bf_rel, parents=True, exist_ok=True)
        bf_json = build_backupfolder_json(
            folder_uuid=folder_uuid, name=folder_name,
            local_path=str(source),
            local_mount_point=local_mount_point,
        )
        self.backend.write_all(
            f"{bf_rel}/backupfolder.json",
            json.dumps(bf_json, indent=2).encode("utf-8"),
        )

        # Update accumulated plan metadata before walking so the
        # backuprecord we emit references the new folder plan too.
        plan_entry = build_folder_plan(
            folder_uuid=folder_uuid,
            local_path=str(source),
            name=folder_name,
            local_mount_point=local_mount_point,
        )
        self._folder_plans.append(plan_entry)
        self._write_plan_json()

        # Build the prior-tree index for this folder if dedup is on
        # and the keyset was reused (latter implies blob_ids will
        # line up so the prior dataBlobLocs are still valid).
        if self.dedup_against_existing and self._keyset_was_reused:
            from .prior_tree import PriorTreeIndex
            idx = PriorTreeIndex(
                self.dest_root, self.computer_uuid,
                self.encryption_key, self.hmac_key,
                folder_uuid=folder_uuid,
                openssl_path=self.openssl_path,
                backend=self.backend,
            )
            self._prior_tree = idx if idx.is_usable else None
            if self._prior_tree is not None:
                _emit(self.callback, "prior_tree_loaded",
                      folder_uuid=folder_uuid)
        else:
            self._prior_tree = None

        # Walk + write blobs.
        root_node, _size, _count = self._walk(source)

        # Flush any in-flight packs BEFORE building the backuprecord.
        # The record embeds BlobLocs whose offsets must point at
        # already-on-disk bytes; flushing here guarantees that.
        self.flush_packs()

        # Build the backup plan dict (snapshot embedded in the record).
        plan_dict = build_backupplan(
            plan_uuid=self.plan_uuid,
            plan_name=self.plan_name,
            folder_plans=self._folder_plans,
            is_encrypted=True,
            creation_time=time.time(),
            update_time=time.time(),
        )

        # Backup record path: backuprecords/<5-digit-bucket>/<num>.backuprecord.
        # Bucket = floor(creation_date / 100000) (zero-padded to 5
        # digits). Filename = (creation_date % 100000).backuprecord.
        creation_date = int(time.time())
        bucket = f"{creation_date // 100000:05d}"
        rec_num = creation_date % 100000
        rec_dir_rel = (
            f"{bf_rel}/{BACKUPRECORDS_DIR}/{bucket}"
        )
        self.backend.mkdir(rec_dir_rel, parents=True, exist_ok=True)
        rec_rel = f"{rec_dir_rel}/{rec_num}.backuprecord"
        rec_relative = (
            f"/{self.computer_uuid}/{BACKUPFOLDERS_DIR}/{folder_uuid}/"
            f"{BACKUPRECORDS_DIR}/{bucket}/{rec_num}.backuprecord"
        )

        record_dict = build_backuprecord_dict(
            backup_folder_uuid=folder_uuid,
            backup_plan_uuid=self.plan_uuid,
            backup_plan_dict=plan_dict,
            root_node=root_node,
            local_path=str(source),
            local_mount_point=local_mount_point,
            relative_path=rec_relative,
            creation_date=float(creation_date),
        )
        arqo = build_backuprecord_arqo(
            record_dict,
            encryption_key=self.encryption_key,
            hmac_key=self.hmac_key,
            openssl_path=self.openssl_path,
        )
        self.backend.write_all(rec_rel, arqo)
        self.bytes_on_disk += len(arqo)
        _emit(self.callback, "backuprecord_written",
              path=rec_rel, size=len(arqo),
              folder_uuid=folder_uuid)
        # Compose a Path representation for the legacy return type.
        # Callers using a non-LocalBackend should rely on rec_rel
        # (the backend-relative path) instead.
        if hasattr(self.backend, "root"):
            try:
                rec_path = Path(self.backend.root) / rec_rel.lstrip("/")
            except TypeError:
                rec_path = Path(rec_rel)
        else:
            rec_path = Path(rec_rel)
        return rec_path


def build_backup(
    source: Path,
    dest_root: Path,
    encryption_password: str,
    *,
    backup_name: str = "TUI backup",
    folder_name: Optional[str] = None,
    callback: Optional[ProgressCb] = None,
    openssl_path: str = "openssl",
    computer_uuid: Optional[str] = None,
    plan_uuid: Optional[str] = None,
    folder_uuid: Optional[str] = None,
    use_packs: bool = False,
    max_pack_bytes: int = DEFAULT_MAX_PACK_BYTES,
    chunker_config: Optional[ChunkerConfig] = None,
    dedup_against_existing: bool = False,
) -> BackupResult:
    """One-shot convenience wrapper: full plan init + single folder.

    ``use_packs=True`` switches blob storage from
    ``standardobjects/`` to the Arq 7 ``treepacks/`` + ``blobpacks/``
    container layout. The reader handles both transparently.

    ``chunker_config`` enables Buzhash content-defined chunking; when
    present, file content is split into variable-length chunks
    according to the rolling hash, each becoming its own ``BlobLoc``.
    Restore concatenates them in order. See
    :mod:`arq_writer.chunker` for the trade-offs.

    ``dedup_against_existing=True`` seeds the within-run blob-id
    cache from the destination's existing ``standardobjects/`` and
    most-recent backuprecord. Subsequent runs skip re-encrypting +
    re-writing any blob whose content (and therefore SHA-256
    ``blob_id``) is already on disk. Backup correctness is unchanged
    either way; this flag only affects I/O cost on incremental
    re-runs against the same destination.
    """
    started = time.time()
    bk = Backup(
        dest_root=dest_root,
        encryption_password=encryption_password,
        backup_name=backup_name,
        callback=callback,
        openssl_path=openssl_path,
        computer_uuid=computer_uuid,
        plan_uuid=plan_uuid,
        use_packs=use_packs,
        max_pack_bytes=max_pack_bytes,
        chunker_config=chunker_config,
        dedup_against_existing=dedup_against_existing,
    )
    bk.init_plan()
    rec_path = bk.add_folder(
        Path(source), folder_uuid=folder_uuid, folder_name=folder_name,
    )
    finished = time.time()
    return BackupResult(
        dest_root=Path(dest_root).resolve(),
        computer_uuid=bk.computer_uuid,
        plan_uuid=bk.plan_uuid,
        folder_uuid=rec_path.parent.parent.parent.name,
        backuprecord_path=rec_path,
        files_written=bk.files_written,
        trees_written=bk.trees_written,
        bytes_plaintext=bk.bytes_plaintext,
        bytes_on_disk=bk.bytes_on_disk,
        blob_ids=list(bk.blob_ids),
        started_at=started,
        finished_at=finished,
    )
