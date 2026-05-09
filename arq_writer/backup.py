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
    DEFAULT_MAX_PACKED_ITEM_LENGTH,
    KEYSET_FILE,
    KEYSET_PLAIN_FIELD_LEN,
    LARGEBLOBPACKS_DIR,
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
from .xattrs import capture_xattrs, serialize_xattrs


ProgressCb = Callable[[str, dict], None]


class BackupCancelled(RuntimeError):
    """Raised inside the writer when ``Backup.cancel()`` is invoked.

    Caught by ``add_folder`` so the partial walk doesn't produce a
    half-written backuprecord. Pack files that already flushed in
    full pack-bytes batches stay on disk (they're a strict subset
    of valid blobs); the in-memory pack buffer is dropped.
    """


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


def _resolve_owner(
    uid: int, gid: int,
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(username, group_name)`` for a uid/gid pair.

    Arq.app's backuprecord ``node`` dict carries ``userName`` and
    ``groupName`` alongside the numeric ``mac_st_uid``/``mac_st_gid``;
    leaving them off makes the round-trip diverge from Arq.app and
    blocks GUI ownership UI. We resolve via stdlib ``pwd``/``grp``
    on POSIX systems; lookup misses (uid not in passwd, e.g.
    LDAP-only environments) and Windows (no ``pwd``/``grp``)
    return ``None`` so the writer emits an empty string per
    Arq.app convention.
    """
    username = None
    group_name = None
    try:
        import pwd  # POSIX-only
        try:
            username = pwd.getpwuid(uid).pw_name
        except (KeyError, OSError):
            pass
    except ImportError:
        pass
    try:
        import grp  # POSIX-only
        try:
            group_name = grp.getgrgid(gid).gr_name
        except (KeyError, OSError):
            pass
    except ImportError:
        pass
    return username, group_name


def _empty_skipped_filenode() -> "FileNode":
    """Stand-in FileNode for entries excluded by source filters.

    Returned by ``_walk_file`` when a file is dropped by a size
    limit or exclusion rule. The Tree's parent walker still
    needs *something* node-shaped to attach so the walk doesn't
    cascade into a NoneType crash; we surface a 0-byte FileNode
    with no dataBlobLocs and the ``deleted`` flag set so a
    restorer that someday does honor the flag treats it as a
    tombstone instead of an empty file.
    """
    return FileNode(
        dataBlobLocs=[],
        itemSize=0,
        containedFilesCount=0,
        deleted=True,
    )


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
        large_blob_threshold: int = DEFAULT_MAX_PACKED_ITEM_LENGTH,
        chunker_config: Optional[ChunkerConfig] = None,
        dedup_against_existing: bool = False,
        max_file_bytes: Optional[int] = None,
        exclusions=None,
        backend=None,
        callback: Optional[ProgressCb] = None,
        tree_version: int = TREE_VERSION,
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
        # Tree binary version this writer emits. Default = 3 (the
        # version the Arq 7 spec documents). Pass 4 to also emit
        # the 38-byte trailing block per Node we discovered in
        # Arq.app v8 destinations (see
        # docs/REAL-DATA-DISCOVERIES.md §7); the reader handles
        # both versions transparently. Existing unit tests assume
        # 3, so we keep that as the default and let opt-ins flow
        # through this knob.
        self.tree_version = int(tree_version)
        self.max_pack_bytes = max_pack_bytes
        self.large_blob_threshold = large_blob_threshold
        self.chunker_config = chunker_config
        self._chunker: Optional[Buzhash] = (
            Buzhash(chunker_config) if chunker_config is not None else None
        )
        self.dedup_against_existing = dedup_against_existing
        self.max_file_bytes = max_file_bytes
        # Default to an empty (= no-op) ExclusionRules when not
        # supplied so _walk_dir can call .excludes(...) freely.
        from .exclusions import ExclusionRules
        self.exclusions = exclusions if exclusions is not None else ExclusionRules.empty()
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
        # Hardlink cache: (st_dev, st_ino) → already-walked FileNode.
        # Sources with many hardlinks (git checkouts, node_modules)
        # share inode across many path entries; without this the
        # walker re-reads + re-chunks + re-encrypts the same bytes
        # for each link. With it we hit a single FileNode build per
        # inode, then return that same Node for the rest of the
        # links — restore-side hardlink reconstruction relies on
        # the matching mac_st_ino to recreate the link relationship.
        self._inode_to_node: Dict[Tuple[int, int], FileNode] = {}

        # Per-family pack builders, lazily initialized on first use
        # to avoid creating empty pack-shaped paths when use_packs is
        # disabled.
        self._blob_pack: Optional[PackBuilder] = None
        self._tree_pack: Optional[PackBuilder] = None
        self._large_blob_pack: Optional[PackBuilder] = None

        # Prior-tree index for tree-walk reuse (cross-run skip of
        # read+chunk on unchanged files). Populated lazily in
        # add_folder() per folder_uuid; None means "no prior backup
        # for this folder, walk normally".
        self._prior_tree = None

        # Cooperative cancellation. When set, _walk and _walk_dir
        # check the flag at every directory boundary and bail out
        # by raising BackupCancelled. add_folder catches that to
        # produce a partial result (no backuprecord written) so the
        # destination's prior state is unchanged.
        self._cancelled = False
        # Pause/resume scaffolding. ``_paused`` is the requested
        # state — set by another thread via :meth:`pause` /
        # :meth:`resume`. The walker observes it at every
        # directory boundary + start of each file walk via
        # :meth:`_check_cancel` (which now also handles pause)
        # and blocks until resumed. Distinct from cancel so the
        # operator can pause a long backup, do something else,
        # and resume without losing in-flight state.
        self._paused = False
        # Sleep granularity inside the pause spin. 0.5s is fast
        # enough that a resume feels immediate but slow enough
        # that the walker doesn't burn CPU while paused.
        self._pause_poll_sec = 0.5

    def cancel(self) -> None:
        """Request graceful cancellation of an in-flight backup.

        Thread-safe: meant to be invoked from a different thread
        than the one running ``add_folder`` / ``build_backup``.
        The cancellation is checked at every directory boundary
        and at the start of each file walk; once observed the
        backup raises :class:`BackupCancelled`, ``add_folder``
        skips writing the backuprecord, and pack files are
        flushed only if some content was already buffered.
        """
        self._cancelled = True
        # A pending cancel takes precedence over a pending pause —
        # un-pause so the walker wakes from its spin and sees
        # _cancelled.
        self._paused = False

    def pause(self) -> None:
        """Request the walker to suspend at the next checkpoint.

        Thread-safe (same contract as :meth:`cancel`). The walker
        will keep the in-memory state intact and spin in
        :meth:`_check_cancel` until :meth:`resume` is called.
        Pack-file buffers stay in memory; no flush happens during
        pause, so resuming continues the same pack instead of
        starting a fresh one.
        """
        self._paused = True
        _emit(self.callback, "backup_paused")

    def resume(self) -> None:
        """Lift a previously-set pause flag. No-op if not paused."""
        if self._paused:
            self._paused = False
            _emit(self.callback, "backup_resumed")

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _check_cancel(self) -> None:
        # Pause first — paused walkers shouldn't see cancel-style
        # exceptions purely from being mid-spin when the cancel
        # arrives.
        while self._paused and not self._cancelled:
            import time as _time
            _time.sleep(self._pause_poll_sec)
        if self._cancelled:
            raise BackupCancelled(
                "backup cancelled by Backup.cancel()"
            )

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
            json.dumps(
                config, indent=2, ensure_ascii=False,
            ).encode("utf-8"),
        )

        folders_idx = build_backupfolders_json(self.computer_uuid)
        self.backend.write_all(
            self._cu_path("backupfolders.json"),
            json.dumps(
                folders_idx, indent=2, ensure_ascii=False,
            ).encode("utf-8"),
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
            json.dumps(
                plan, indent=2, ensure_ascii=False,
            ).encode("utf-8"),
        )

    # ------------------------------------------------------------------
    # Blob writing
    # ------------------------------------------------------------------

    def _xattr_locs_for(self, src: Path) -> List[BlobLoc]:
        """Capture every xattr on ``src`` and return the BlobLoc list
        to attach to the corresponding Node.

        Returns an empty list when the entry has no xattrs OR the
        host/Python doesn't expose xattr APIs (Windows, stripped
        builds). Otherwise serializes all of them into one
        binary-plist blob — see ``arq_writer/xattrs.py`` for the
        rationale of the single-blob-per-Node format.
        """
        try:
            xattrs = capture_xattrs(src, callback=lambda kind, payload:
                                    _emit(self.callback, kind, **payload))
        except Exception as exc:
            # capture_xattrs already swallows expected OSErrors; if
            # something else escapes, log + continue rather than
            # blocking the whole walk on one entry.
            _emit(self.callback, "xattr_capture_error",
                  path=str(src), error=str(exc))
            return []
        if not xattrs:
            return []
        blob = serialize_xattrs(xattrs)
        if not blob:
            return []
        return [self._write_blob(blob)]

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
            elif (
                self.large_blob_threshold > 0
                and len(arqo) > self.large_blob_threshold
            ):
                # spec: blobs whose ARQO bytes exceed
                # maxPackedItemLength go to largeblobpacks/ instead
                # of blobpacks/. Matches Arq.app's routing exactly.
                if self._large_blob_pack is None:
                    self._large_blob_pack = PackBuilder(
                        self.computer_uuid, LARGEBLOBPACKS_DIR,
                        self.dest_root,
                        max_pack_bytes=self.max_pack_bytes,
                        compression_type=COMPRESSION_LZ4,
                        backend=self.backend,
                    )
                loc = self._large_blob_pack.add(blob_id, arqo)
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
        if self._large_blob_pack is not None:
            self._large_blob_pack.close()

    # ------------------------------------------------------------------
    # Folder walk
    # ------------------------------------------------------------------

    def _walk(
        self, source: Path, rel_path: str = "",
    ) -> "Optional[Tuple[Node, int, int]]":
        """Recursively encode ``source`` into a Node + write blobs.

        ``rel_path`` is the source-root-relative POSIX path of
        ``source`` (empty string for the root). It's used by the
        prior-tree index to look up previously-recorded FileNodes
        and skip re-reading unchanged content.

        Returns ``(node, item_size, contained_files_count)``, or
        ``None`` if the entry was skipped (file-size limit, etc.).

        Symlinks are NOT followed; the writer stores the link
        target string under the S_IFLNK mode bit so the restorer
        can recreate them as symlinks rather than regular files.
        """
        self._check_cancel()
        if source.is_dir() and not source.is_symlink():
            return self._walk_dir(source, rel_path=rel_path)
        return self._walk_file(source, rel_path=rel_path)

    def _walk_file(
        self, src: Path, rel_path: str = "",
    ) -> "Optional[Tuple[FileNode, int, int]]":
        """Process one source file. Returns ``None`` when the file
        is skipped (size limit / exclusion); the parent ``_walk_dir``
        drops ``None`` results from its children list."""
        # File-size skip rule. Symlinks always pass (their lstat
        # size is just the link-target string length).
        if (
            self.max_file_bytes is not None
            and not src.is_symlink()
        ):
            try:
                size = src.stat().st_size
            except OSError:
                size = 0
            if size > self.max_file_bytes:
                _emit(self.callback, "file_skipped",
                      path=str(src), rel_path=rel_path,
                      reason="size_limit",
                      size=size,
                      limit=self.max_file_bytes)
                return None
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
        # Hardlink dedup. Sources with many hardlinks (a git
        # checkout, a node_modules tree) share an inode across
        # many path entries; without this short-circuit the
        # walker re-reads + re-chunks + re-encrypts the same
        # bytes for each link. Symlinks are excluded because
        # st_nlink semantics there are murky (the link itself,
        # not the target, is what we're recording).
        is_symlink_for_dedup = src.is_symlink()
        if not is_symlink_for_dedup:
            try:
                st_for_link = src.stat()
            except OSError:
                st_for_link = None
            if (
                st_for_link is not None
                and getattr(st_for_link, "st_nlink", 1) > 1
            ):
                key = (
                    int(getattr(st_for_link, "st_dev", 0) or 0),
                    int(getattr(st_for_link, "st_ino", 0) or 0),
                )
                if key[1]:
                    cached = self._inode_to_node.get(key)
                    if cached is not None:
                        # Re-use the same FileNode (binary-identical
                        # bytes → tree blob_id stays stable, dedup
                        # holds across runs). Restore-side hardlink
                        # reconstruction reads mac_st_ino off the
                        # node and groups equal-inode entries.
                        _emit(self.callback, "file_hardlinked",
                              path=str(src),
                              rel_path=rel_path,
                              inode=key[1])
                        self.files_written += 1
                        return (
                            cached,
                            int(getattr(cached, "itemSize", 0)),
                            1,
                        )
        # Capture extended attributes once per entry; one
        # consolidated blob per Node keeps the BlobLoc count
        # bounded regardless of how many xattrs the file has.
        # See arq_writer/xattrs.py for the format choice.
        xattr_locs = self._xattr_locs_for(src)
        # Symlinks: don't follow. Store the link target string as
        # the file's content so the restorer can rebuild the link
        # under the S_IFLNK mode bit.
        is_symlink = src.is_symlink()
        if is_symlink:
            try:
                data = os.readlink(src).encode("utf-8")
            except OSError as exc:
                _emit(self.callback, "file_read_error",
                      path=str(src), error=str(exc))
                data = b""
            # lstat so we get the symlink's own metadata, not the
            # target's.
            st = src.lstat()
            locs = [self._write_blob(data)]
            uname, gname = _resolve_owner(
                st.st_uid if hasattr(st, "st_uid") else 0,
                st.st_gid if hasattr(st, "st_gid") else 0,
            )
            node = FileNode(
                dataBlobLocs=locs,
                xattrsBlobLocs=xattr_locs,
                itemSize=len(data),
                containedFilesCount=1,
                mtime_sec=int(st.st_mtime),
                mtime_nsec=int(
                    (st.st_mtime - int(st.st_mtime)) * 1_000_000_000
                ),
                ctime_sec=int(st.st_ctime),
                ctime_nsec=int(
                    (st.st_ctime - int(st.st_ctime)) * 1_000_000_000
                ),
                username=uname,
                groupName=gname,
                mac_st_mode=st.st_mode,
                mac_st_uid=st.st_uid if hasattr(st, "st_uid") else 0,
                mac_st_gid=st.st_gid if hasattr(st, "st_gid") else 0,
                mac_st_ino=st.st_ino,
                mac_st_nlink=st.st_nlink,
            )
            self.files_written += 1
            _emit(self.callback, "file_written",
                  path=str(src), size=len(data),
                  chunks=1, symlink=True,
                  blob_id=locs[0].blobIdentifier if locs else "")
            return node, len(data), 1

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
        uname, gname = _resolve_owner(
            st.st_uid if hasattr(st, "st_uid") else 0,
            st.st_gid if hasattr(st, "st_gid") else 0,
        )
        node = FileNode(
            dataBlobLocs=locs,
            xattrsBlobLocs=xattr_locs,
            itemSize=len(data),
            containedFilesCount=1,
            mtime_sec=int(st.st_mtime),
            mtime_nsec=int((st.st_mtime - int(st.st_mtime)) * 1_000_000_000),
            ctime_sec=int(st.st_ctime),
            ctime_nsec=int((st.st_ctime - int(st.st_ctime)) * 1_000_000_000),
            username=uname,
            groupName=gname,
            mac_st_mode=st.st_mode,
            mac_st_uid=st.st_uid if hasattr(st, "st_uid") else 0,
            mac_st_gid=st.st_gid if hasattr(st, "st_gid") else 0,
            mac_st_ino=st.st_ino,
            mac_st_nlink=st.st_nlink,
        )
        # Cache for the next link of the same inode in this run.
        if int(getattr(st, "st_nlink", 1) or 1) > 1:
            self._inode_to_node[(
                int(getattr(st, "st_dev", 0) or 0),
                int(getattr(st, "st_ino", 0) or 0),
            )] = node
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
            # Exclusion filter — drop the whole entry (file or
            # subtree) before any I/O.
            if not self.exclusions.is_empty:
                is_dir = entry.is_dir() and not entry.is_symlink()
                if self.exclusions.excludes(child_rel, is_dir=is_dir):
                    _emit(self.callback, "entry_excluded",
                          path=str(entry), rel_path=child_rel,
                          is_dir=is_dir)
                    continue
            walked = self._walk(entry, rel_path=child_rel)
            if walked is None:
                # File was skipped (size limit, etc.).
                continue
            child_node, child_size, child_count = walked
            children.append(TreeChild(name=entry.name, node=child_node))
            item_size += child_size
            contained += child_count

        tree = Tree(children=children, version=self.tree_version)
        tree_bytes = write_tree(tree, version=self.tree_version)
        tree_loc = self._write_blob(tree_bytes, is_tree=True)
        self.trees_written += 1

        st = src.stat()
        # Directories can carry xattrs too (Finder labels, ACL
        # markers, etc.). Capture them on the TreeNode itself so
        # restore can re-apply them after mkdir.
        dir_xattr_locs = self._xattr_locs_for(src)
        node = TreeNode(
            treeBlobLoc=tree_loc,
            xattrsBlobLocs=dir_xattr_locs,
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
        chunker_config: Optional[ChunkerConfig] = None,
    ) -> Path:
        """Walk ``source`` and write the resulting backuprecord.

        Returns the absolute path to the written backuprecord file.
        Multiple calls accumulate into the same plan; ``backupplan.json``
        is rewritten each time so it stays in sync.

        ``chunker_config`` overrides the constructor-level chunker
        for this folder only — matches Arq.app's per-folder
        ``useBuzhash`` toggle. When set, the override applies to
        every blob written during this ``add_folder`` call and the
        instance reverts to the default chunker afterward.
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
            json.dumps(
                bf_json, indent=2, ensure_ascii=False,
            ).encode("utf-8"),
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

        # Per-folder chunker override — swap in for the duration
        # of this folder's walk, then restore.
        prev_chunker = self._chunker
        if chunker_config is not None:
            self._chunker = Buzhash(chunker_config)

        # Walk + write blobs. A cooperative cancel raised mid-walk
        # short-circuits to "no backuprecord written"; the partial
        # state we leave behind is just orphan blobs / packs, all
        # of which the next dedup-against-existing run will pick up
        # again.
        try:
            walked = self._walk(source)
            if walked is None:
                # Whole source root excluded? Emit empty TreeNode
                # so the backuprecord still has well-defined shape.
                from .types import Tree
                empty_tree = Tree(children=[], version=self.tree_version)
                tree_loc = self._write_blob(
                    write_tree(empty_tree, version=self.tree_version),
                    is_tree=True,
                )
                root_node = TreeNode(
                    treeBlobLoc=tree_loc, itemSize=0,
                    containedFilesCount=0,
                )
            else:
                root_node, _size, _count = walked
        except BackupCancelled:
            _emit(self.callback, "backup_cancelled",
                  folder_uuid=folder_uuid)
            raise

        # Flush any in-flight packs BEFORE building the backuprecord.
        # The record embeds BlobLocs whose offsets must point at
        # already-on-disk bytes; flushing here guarantees that.
        self.flush_packs()

        # Restore the constructor-level chunker if this folder
        # used an override.
        if chunker_config is not None:
            self._chunker = prev_chunker

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
    large_blob_threshold: int = DEFAULT_MAX_PACKED_ITEM_LENGTH,
    chunker_config: Optional[ChunkerConfig] = None,
    dedup_against_existing: bool = False,
    max_file_bytes: Optional[int] = None,
    exclusions=None,
    use_apfs_snapshot: bool = False,
    tree_version: int = TREE_VERSION,
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
        large_blob_threshold=large_blob_threshold,
        chunker_config=chunker_config,
        dedup_against_existing=dedup_against_existing,
        max_file_bytes=max_file_bytes,
        exclusions=exclusions,
        tree_version=tree_version,
    )
    bk.init_plan()

    # Optional: walk an APFS snapshot of the source instead of the
    # live filesystem, so file content can't shift mid-walk. macOS
    # only — falls through to live source on Linux / non-APFS.
    if use_apfs_snapshot:
        from .macos_snapshot import (
            NotMacOSError, with_apfs_snapshot,
        )
        try:
            with with_apfs_snapshot(Path(source)) as snap_path:
                rec_path = bk.add_folder(
                    snap_path,
                    folder_uuid=folder_uuid,
                    folder_name=(
                        folder_name or Path(source).name
                    ),
                )
        except NotMacOSError:
            # Fall back to the live walk — opt-in only enables the
            # snapshot when supported, doesn't fail otherwise.
            _emit(callback, "apfs_snapshot_skipped",
                  reason="not_macos",
                  source=str(source))
            rec_path = bk.add_folder(
                Path(source),
                folder_uuid=folder_uuid,
                folder_name=folder_name,
            )
    else:
        rec_path = bk.add_folder(
            Path(source),
            folder_uuid=folder_uuid,
            folder_name=folder_name,
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
