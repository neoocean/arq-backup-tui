"""Arq 7 pack file emitter.

The Arq 7 pack-file format is **plain concatenation of EncryptedObject
blobs** with no per-entry framing — confirmed by reading
``arq_restore/arq7restore/Arq7BlobReader.m::dataForBlobLoc:``, which
extracts pack-stored blobs via ``pack[offset:offset+length]`` and
treats the result as an ARQO directly. There is no per-entry length /
mime / name header (those exist in the legacy Arq 5/6 ``.pack``
format but are absent from Arq 7).

A :class:`PackBuilder` accumulates ARQOs in an in-memory buffer and
flushes to disk when the buffer reaches a configurable threshold
(default 10 MiB — well below Arq.app's default 64 MiB bucket but
enough to make the per-pack file count manageable). Each ``add()``
call returns a fully-formed :class:`BlobLoc` pointing into the
**current** pack file, so callers can build Trees + backuprecords as
the walk proceeds without needing a deferred relocation pass.

Pack file naming follows Arq 7's convention exactly: a UUID where the
first two hex characters become the shard directory, and the
remaining 30 hex / 3 dashes form the filename. The validator's
``ARQ7_PACK_NAME_RE`` accepts what we emit.

This module covers ``treepacks/``, ``blobpacks/``, and
``largeblobpacks/`` — the three packed object families the validator
already discovers. ``standardobjects/`` remains a separate code path
because per-spec it stores blobs *unpacked*; large file content goes
to ``largeblobpacks/`` instead.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .types import BlobLoc

# Default pack-file size threshold before flushing to disk. 10 MiB
# stays well under Arq.app's observed cap (~64 MiB on operator
# destinations) while keeping the per-pack file count proportional
# to the backup size — a 1 GB backup yields ~100 pack files at the
# default. Operators can override per Backup instance.
DEFAULT_MAX_PACK_BYTES = 10 * 1024 * 1024


@dataclass
class PackFileInfo:
    """One emitted ``.pack`` file."""

    relative_path: str             # e.g. /<cu>/blobpacks/00/036BE7-...pack
    size: int
    blob_count: int
    blob_ids: List[str] = field(default_factory=list)


def _allocate_pack_path(computer_uuid: str, family: str) -> str:
    """Generate a fresh Arq-7-shaped pack path.

    The pack ID is a UUID; first 2 hex chars become the shard, the
    rest (with dashes) is the filename. Example:

        /<cu>/blobpacks/03/6BE7AC-B92F-4FCF-A762-EB829DCE7EC3.pack

    The validator's ``ARQ7_PACK_NAME_RE`` matches exactly this shape.
    """
    pack_id = str(uuid.uuid4()).upper()  # 36 chars: 8-4-4-4-12
    shard = pack_id[:2]
    name = pack_id[2:] + ".pack"          # 33 chars + ".pack"
    return f"/{computer_uuid}/{family}/{shard}/{name}"


class PackBuilder:
    """Per-family in-memory buffer that flushes ``.pack`` files on
    threshold.

    Use one instance per object family (typically one for trees, one
    for blobs, optionally one for large blobs). Call :meth:`add` to
    append an ARQO and receive a back-pointing BlobLoc; call
    :meth:`close` at the end of the backup so any buffered content
    is flushed.

    Dedup: ``add(blob_id, arqo)`` is idempotent — repeated calls with
    the same ``blob_id`` return the cached BlobLoc without rewriting.
    """

    def __init__(
        self,
        computer_uuid: str,
        family: str,
        dest_root: Path,
        *,
        max_pack_bytes: int = DEFAULT_MAX_PACK_BYTES,
        compression_type: int = 2,
    ) -> None:
        self.computer_uuid = computer_uuid
        self.family = family
        self.dest_root = Path(dest_root).resolve()
        self.max_pack_bytes = max_pack_bytes
        self.compression_type = compression_type

        self._buffer = bytearray()
        self._current_relative_path = _allocate_pack_path(
            computer_uuid, family,
        )
        self._current_blob_ids: List[str] = []
        self._cache: Dict[str, BlobLoc] = {}
        self.packs_written: List[PackFileInfo] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, blob_id: str, arqo: bytes) -> BlobLoc:
        """Append ``arqo`` to the current pack and return its BlobLoc.

        If a previous call recorded the same ``blob_id``, the cached
        BlobLoc is returned and ``arqo`` is ignored — same dedup
        semantics as the standalone-objects path.

        Triggers a flush if appending would push the buffer past the
        configured threshold AND the buffer is non-empty (so we never
        waste a flush on an empty pack just to make room for a single
        oversized blob).
        """
        cached = self._cache.get(blob_id)
        if cached is not None:
            return cached

        if self._buffer and len(self._buffer) + len(arqo) > self.max_pack_bytes:
            self._flush()

        offset = len(self._buffer)
        self._buffer += arqo
        self._current_blob_ids.append(blob_id)
        loc = BlobLoc(
            blobIdentifier=blob_id,
            isPacked=True,
            relativePath=self._current_relative_path,
            offset=offset,
            length=len(arqo),
            stretchEncryptionKey=True,
            compressionType=self.compression_type,
        )
        self._cache[blob_id] = loc
        return loc

    def close(self) -> List[PackFileInfo]:
        """Flush the in-memory buffer if non-empty and return all
        :class:`PackFileInfo` entries written across this builder's
        lifetime.
        """
        if self._buffer:
            self._flush()
        return list(self.packs_written)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        path = self.dest_root / self._current_relative_path.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        body = bytes(self._buffer)
        path.write_bytes(body)
        self.packs_written.append(PackFileInfo(
            relative_path=self._current_relative_path,
            size=len(body),
            blob_count=len(self._current_blob_ids),
            blob_ids=list(self._current_blob_ids),
        ))
        self._buffer = bytearray()
        self._current_blob_ids = []
        self._current_relative_path = _allocate_pack_path(
            self.computer_uuid, self.family,
        )
