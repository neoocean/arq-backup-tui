"""Arq 7 directory-layout discovery.

A backup destination contains one or more *computer UUID* subtrees,
each of which has the canonical Arq 7 layout:

    <computer-uuid>/
        encryptedkeyset.dat
        blobpacks/<2-hex-shard>/<UUID>.pack
        treepacks/<2-hex-shard>/<UUID>.pack
        largeblobpacks/<2-hex-shard>/<UUID>.pack
        standardobjects/<2-hex-shard>/<62-hex-name>
        backupfolders/<folder-uuid>/backuprecords/<NNNNN>/<num>.backuprecord

This module walks that tree and produces a structured snapshot used by
all downstream validation tiers.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import constants as C
from .backend import Backend


@dataclass
class Arq7ComputerLayout:
    """One computer-UUID subtree's discovered structure."""

    computer_uuid: str
    blobpacks: List[Tuple[str, str]] = field(default_factory=list)
    treepacks: List[Tuple[str, str]] = field(default_factory=list)
    largeblobpacks: List[Tuple[str, str]] = field(default_factory=list)
    standardobjects: List[Tuple[str, str]] = field(default_factory=list)
    backup_folder_uuids: List[str] = field(default_factory=list)
    has_keyset: bool = False

    def total_objects(self) -> int:
        return (
            len(self.blobpacks)
            + len(self.treepacks)
            + len(self.largeblobpacks)
            + len(self.standardobjects)
        )

    def family_items(self, kind: str) -> List[Tuple[str, str]]:
        return {
            C.BLOBPACKS_DIR: self.blobpacks,
            C.TREEPACKS_DIR: self.treepacks,
            C.LARGEBLOBPACKS_DIR: self.largeblobpacks,
            C.STANDARDOBJECTS_DIR: self.standardobjects,
        }[kind]


def computer_root(root: str, computer_uuid: str) -> str:
    return f"{root.rstrip('/')}/{computer_uuid}"


def object_path(
    root: str, computer_uuid: str, kind: str, shard: str, file_name: str,
) -> str:
    return f"{computer_root(root, computer_uuid)}/{kind}/{shard}/{file_name}"


def keyset_path(root: str, computer_uuid: str) -> str:
    return f"{computer_root(root, computer_uuid)}/{C.KEYSET_FILE}"


def _enumerate_sharded_dir(
    backend: Backend,
    base_path: str,
    name_re,
    *,
    concurrency: int,
) -> List[Tuple[str, str]]:
    """Walk ``base_path/<2-hex-shard>/`` for files matching ``name_re``."""
    if not backend.is_dir(base_path):
        return []
    try:
        shards = backend.list_dir(base_path)
    except (OSError, RuntimeError):
        return []
    valid_shards = [
        s for s in shards
        if 1 <= len(s) <= 2 and all(c in "0123456789abcdefABCDEF" for c in s)
    ]
    if not valid_shards:
        return []

    def _list_one(shard: str) -> Tuple[str, List[str]]:
        try:
            return shard, backend.list_dir(f"{base_path}/{shard}")
        except (OSError, RuntimeError):
            return shard, []

    out: List[Tuple[str, str]] = []
    if concurrency <= 1 or len(valid_shards) == 1:
        for shard in valid_shards:
            _, files = _list_one(shard)
            for fn in files:
                if name_re.match(fn):
                    out.append((shard, fn))
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(_list_one, s) for s in valid_shards]
            for fut in as_completed(futures):
                shard, files = fut.result()
                for fn in files:
                    if name_re.match(fn):
                        out.append((shard, fn))
    out.sort()
    return out


def discover_layout(
    backend: Backend, root: str = "/", *,
    concurrency: int = 8,
    enumerate_objects: bool = True,
) -> List[Arq7ComputerLayout]:
    """Discover all Arq 7 computer subtrees under ``root``.

    Returns one :class:`Arq7ComputerLayout` per discovered computer
    UUID. ``backup_folder_uuids`` is top-level only — the nested
    ``backuprecords/<NNNNN>/<num>.backuprecord`` structure is walked
    on-demand by the L1b tier so cheap modes don't pay the cost.

    ``enumerate_objects=False`` skips the per-shard ``list_dir`` walk
    of ``standardobjects/``, ``treepacks/``, ``blobpacks/``, and
    ``largeblobpacks/``. Use it when a caller only needs the
    computer UUID + folder UUIDs (most non-audit codepaths) — on a
    real destination over SFTP the full walk dispatches up to
    ``256 × 4 = 1024`` ``list_dir`` calls and routinely takes
    minutes.
    """
    layouts: List[Arq7ComputerLayout] = []
    if not backend.is_dir(root):
        return layouts
    try:
        entries = backend.list_dir(root)
    except (OSError, RuntimeError):
        return layouts

    for entry in entries:
        if not C.COMPUTER_UUID_RE.match(entry):
            continue
        cu_root = computer_root(root, entry)
        layout = Arq7ComputerLayout(computer_uuid=entry)
        layout.has_keyset = backend.exists(
            f"{cu_root}/{C.KEYSET_FILE}"
        )
        if enumerate_objects:
            layout.blobpacks = _enumerate_sharded_dir(
                backend, f"{cu_root}/{C.BLOBPACKS_DIR}",
                C.PACK_NAME_RE, concurrency=concurrency,
            )
            layout.treepacks = _enumerate_sharded_dir(
                backend, f"{cu_root}/{C.TREEPACKS_DIR}",
                C.PACK_NAME_RE, concurrency=concurrency,
            )
            layout.largeblobpacks = _enumerate_sharded_dir(
                backend, f"{cu_root}/{C.LARGEBLOBPACKS_DIR}",
                C.PACK_NAME_RE, concurrency=concurrency,
            )
            layout.standardobjects = _enumerate_sharded_dir(
                backend, f"{cu_root}/{C.STANDARDOBJECTS_DIR}",
                C.STANDARDOBJECT_NAME_RE, concurrency=concurrency,
            )
        bf_root = f"{cu_root}/{C.BACKUPFOLDERS_DIR}"
        if backend.is_dir(bf_root):
            try:
                folders = backend.list_dir(bf_root)
            except (OSError, RuntimeError):
                folders = []
            layout.backup_folder_uuids = sorted(
                f for f in folders if C.FOLDER_UUID_RE.match(f)
            )
        layouts.append(layout)
    return layouts


def list_backuprecords(
    backend: Backend, root: str, computer_uuid: str, folder_uuid: str,
) -> List[str]:
    """Return every backuprecord path under
    ``backupfolders/<folder>/backuprecords/<bucket>/<num>.backuprecord``,
    sorted oldest-first.

    Arq.app picks ``(bucket, num)`` such that lexicographic ordering
    matches ``creationDate`` order, so paths sorted as strings give
    chronological order — which is what every caller of this
    function actually relies on. The exact ``bucket`` formula is
    Arq.app-internal; empirical evidence from real destinations
    suggests it's closer to ``floor(creationDate / 10_000_000)``
    than the ``/ 100000`` form earlier docs cited, and ``num`` is
    a separate sequence that isn't a simple modulo of
    ``creationDate``. We do not depend on the formula, only on
    the resulting ordering.

    Empty list when the folder has no records.
    """
    base = (
        f"{computer_root(root, computer_uuid)}/{C.BACKUPFOLDERS_DIR}/"
        f"{folder_uuid}/{C.BACKUPRECORDS_DIR}"
    )
    if not backend.is_dir(base):
        return []
    try:
        outer_dirs = backend.list_dir(base)
    except (OSError, RuntimeError):
        return []
    out: List[Tuple[Tuple[str, int], str]] = []
    for outer in outer_dirs:
        if not (outer.isdigit() and len(outer) == 5):
            continue
        outer_path = f"{base}/{outer}"
        try:
            inner = backend.list_dir(outer_path)
        except (OSError, RuntimeError):
            continue
        for n in inner:
            if not n.endswith(".backuprecord"):
                continue
            stem = n[: -len(".backuprecord")]
            if not stem.isdigit():
                continue
            out.append(((outer, int(stem)), f"{outer_path}/{n}"))
    out.sort()
    return [p for _, p in out]


def find_latest_backuprecord(
    backend: Backend, root: str, computer_uuid: str, folder_uuid: str,
) -> Optional[str]:
    """Resolve the absolute path to a folder's most recent backuprecord.

    Arq 7 backuprecord layout (2-level — confirmed on a live destination
    2026-05-05; earlier 3-level reads were a misinterpretation of sftp
    listing single-file targets):

        backupfolders/<folder>/backuprecords/
            <NNNNN>/                       5-digit zero-padded shard
            └── <num>.backuprecord         file

    "Latest" = lexicographically-largest outer dir, then numerically
    largest ``<num>`` within it. Returns ``None`` if no record exists.
    """
    base = (
        f"{computer_root(root, computer_uuid)}/{C.BACKUPFOLDERS_DIR}/"
        f"{folder_uuid}/{C.BACKUPRECORDS_DIR}"
    )
    if not backend.is_dir(base):
        return None
    try:
        outer_dirs = backend.list_dir(base)
    except (OSError, RuntimeError):
        return None
    valid_outer = sorted(
        [d for d in outer_dirs if d.isdigit() and len(d) == 5],
        reverse=True,
    )
    for outer in valid_outer:
        outer_path = f"{base}/{outer}"
        try:
            inner = backend.list_dir(outer_path)
        except (OSError, RuntimeError):
            continue
        ranked: List[Tuple[int, str]] = []
        for n in inner:
            if not n.endswith(".backuprecord"):
                continue
            stem = n[: -len(".backuprecord")]
            if stem.isdigit():
                ranked.append((int(stem), n))
        if not ranked:
            continue
        ranked.sort(reverse=True)
        return f"{outer_path}/{ranked[0][1]}"
    return None
