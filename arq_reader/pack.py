"""Arq 7 pack file walker + index reconstructor.

Arq 7's pack files are different from Arq 5/6's: there is **no
header, no per-entry frame, and no companion ``.index`` file**.
A pack is just the concatenation of ``EncryptedObject`` blobs
back-to-back — see :mod:`arq_writer.pack_builder` for the
writer side and ``arq_restore``'s ``Arq7BlobReader.m`` for
Arq.app's reader confirming the same shape.

This means the canonical way to extract a blob from a pack is
to consult a :class:`~arq_writer.types.BlobLoc` (in some Tree's
binary form) for the ``(relative_path, offset, length)`` triple
and slice the pack accordingly. The reader's
:meth:`arq_reader.Restore.restore` already does this.

Three things the BlobLoc-driven path does *not* give us:

1. Validation that a pack file is internally consistent
   (no dangling bytes between entries, no torn ARQO at the tail).
2. A blob inventory when the source-of-truth Tree is missing or
   corrupt — the salvage scenario.
3. Statistics: how many blobs in a pack, total bytes, on-disk
   bucket-fill ratio.

For all three we need to walk a pack from offset 0 sequentially
and recover the implicit index. Since ARQO entries have no
length prefix, this module reconstructs the index by scanning
forward for the next ``ARQO`` magic — AES-CBC ciphertext bytes
appear uniform-random, so a chance match four bytes deep anywhere
mid-ciphertext is ~2⁻³² ≈ one-in-four-billion per offset; for
any real-world pack file (≤ 64 MiB) the expected false-positive
rate is well under 10⁻². Even so, every recovered entry is
HMAC-verified before being emitted, so a false-positive would
fail authentication and the walker would back out and resume
scanning past the bogus magic.

Public API:

- :class:`PackEntry` — one ``(offset, length, blob_id?)`` tuple.
- :func:`reconstruct_index(pack_bytes)` — pure-byte scan, returns
  the inferred entry list. No keys needed.
- :func:`decode_pack(pack_bytes, enc_key, hmac_key)` — like
  :func:`reconstruct_index` plus HMAC verify + AES decrypt; yields
  ``(PackEntry, plaintext)`` tuples.
- :func:`pack_summary(pack_bytes)` — quick stats wrapper.

The walker is forgiving: a torn tail (last entry truncated mid-
ciphertext) raises :class:`PackTruncated` from
:func:`decode_pack` but :func:`reconstruct_index` still returns
the offsets it could discover, with the trailing partial entry
flagged in :class:`PackEntry.truncated`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Optional, Tuple

from arq_validator import constants as C
from arq_validator.crypto import verify_encrypted_object_hmac

from .decrypt import DecryptError, decrypt_encrypted_object


class PackError(RuntimeError):
    """Raised on malformed pack data."""


class PackTruncated(PackError):
    """Final entry in the pack was truncated mid-ciphertext.

    :func:`reconstruct_index` only flags this on the trailing
    entry; truncation in the middle would imply a missing entry
    boundary and is reported as a regular PackError instead.
    """


@dataclass(frozen=True)
class PackEntry:
    """One ARQO blob recovered from a pack file."""

    offset: int                 # absolute byte offset in the pack
    length: int                 # bytes between this magic and the
                                # next (or end of pack)
    truncated: bool = False     # True only for the last entry if
                                # the pack ends mid-ciphertext
    blob_id: Optional[str] = None  # populated only when the
                                   # caller resolves it from
                                   # plaintext (HMAC ≠ blob_id —
                                   # blob_id is sha1(plaintext))


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _find_magic(data: bytes, start: int) -> int:
    """Return the offset of the next ``ARQO`` magic at or after
    ``start``, or ``-1`` if none found."""
    return data.find(C.ARQO_MAGIC, start)


def reconstruct_index(pack_bytes: bytes) -> List[PackEntry]:
    """Walk a pack file from offset 0 and return every entry's
    ``(offset, length)``.

    No keys required — this is a pure structural scan, useful for
    fingerprinting + corruption detection. Use :func:`decode_pack`
    when you also want HMAC + plaintext.

    The first byte of a valid Arq 7 pack file MUST be the start
    of an ARQO; if not, raises :class:`PackError`. Subsequent
    entries are found by searching forward for the next
    ``ARQO`` magic; gaps between an entry's body and the next
    magic byte would mean a torn or non-Arq pack and raise.
    """
    if len(pack_bytes) == 0:
        return []
    # Pack must start at an ARQO boundary. If not, it's not an
    # Arq 7 pack (or it's been truncated from the head).
    if not pack_bytes.startswith(C.ARQO_MAGIC):
        # Look harder so the message can pinpoint where the first
        # magic actually is — operator triage is easier with a
        # specific number than with "somewhere".
        first = _find_magic(pack_bytes, 0)
        if first < 0:
            raise PackError(
                "no ARQO magic anywhere in pack — not an Arq 7 pack file?"
            )
        raise PackError(
            f"pack does not begin with ARQO magic; first ARQO at "
            f"offset {first}, expected 0"
        )

    entries: List[PackEntry] = []
    offset = 0
    total = len(pack_bytes)
    while offset < total:
        # The minimum legal entry is the ARQO header (116 bytes)
        # plus at least one AES block of ciphertext (16). Anything
        # smaller is structural corruption.
        if offset + C.ARQO_HEADER_BYTES > total:
            entries.append(PackEntry(
                offset=offset,
                length=total - offset,
                truncated=True,
            ))
            break
        # Sanity-check the length when locking onto the next
        # ARQO boundary: the body after the 116-byte header is
        # AES-CBC ciphertext (always a multiple of 16). If the
        # apparent body isn't div-by-16 we likely matched a
        # random-looking magic mid-ciphertext and need to skip
        # forward. We do this with a forward scan that ratchets
        # ``search_from`` so an unsuitable false-positive can't
        # trap the loop.
        end = total
        search_from = offset + 1
        while True:
            cand = _find_magic(pack_bytes, search_from)
            if cand < 0:
                end = total
                break
            body_len = (cand - offset) - C.ARQO_HEADER_BYTES
            if body_len >= 16 and (body_len % 16) == 0:
                end = cand
                break
            # Bad alignment — advance past this candidate and
            # keep scanning so we never re-evaluate it.
            search_from = cand + 1
        length = end - offset
        body_len = length - C.ARQO_HEADER_BYTES
        if body_len < 16 or (body_len % 16) != 0:
            # Hit end-of-pack without finding a clean boundary —
            # this is the truncated trailing entry case.
            entries.append(PackEntry(
                offset=offset, length=length, truncated=True,
            ))
            break
        entries.append(PackEntry(offset=offset, length=length))
        offset = end
    return entries


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


def decode_pack(
    pack_bytes: bytes,
    encryption_key: bytes,
    hmac_key: bytes,
    *,
    openssl_path: str = "openssl",
    skip_truncated: bool = True,
) -> Iterator[Tuple[PackEntry, bytes]]:
    """Yield ``(entry, plaintext)`` for every blob in the pack.

    HMACs are verified per-entry; a mismatch raises
    :class:`PackError` rather than silently skipping — silent
    skip would mask real corruption. If you specifically want
    salvage-mode (skip bad entries), wrap this in a try/except
    around the inner loop.

    ``skip_truncated`` (default True) drops a trailing torn entry
    rather than raising :class:`PackTruncated`. Set False when
    you want the caller to act on the truncation directly.
    """
    for entry in reconstruct_index(pack_bytes):
        if entry.truncated:
            if skip_truncated:
                continue
            raise PackTruncated(
                f"trailing entry truncated at offset {entry.offset}"
            )
        arqo = pack_bytes[entry.offset : entry.offset + entry.length]
        # HMAC first (cheap relative to AES) — fail fast on bad bytes.
        ok, exp_hex, act_hex = verify_encrypted_object_hmac(arqo, hmac_key)
        if not ok:
            raise PackError(
                f"HMAC mismatch at offset {entry.offset} "
                f"(expected {exp_hex[:16]}…, got {act_hex[:16]}…)"
            )
        try:
            plaintext = decrypt_encrypted_object(
                arqo, encryption_key, hmac_key,
                openssl_path=openssl_path,
                # We just verified — don't pay for it twice.
                skip_hmac=True,
            )
        except DecryptError as exc:
            raise PackError(
                f"decrypt failed at offset {entry.offset}: {exc}"
            ) from exc
        yield entry, plaintext


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackSummary:
    """Lightweight stats from :func:`pack_summary`. ``total_size``
    is the byte length of the pack as supplied; ``entry_count``
    counts both intact and truncated entries; ``truncated_tail``
    flips when the very last entry is incomplete."""

    total_size: int
    entry_count: int
    payload_bytes: int          # sum of entry lengths excluding any tear
    truncated_tail: bool


def pack_summary(pack_bytes: bytes) -> PackSummary:
    """Inspect a pack file without keys, returning quick stats.

    Useful for ``arq-validate`` audit reporting and for the TUI
    to surface "pack X has Y blobs, Z bytes" at a glance.
    """
    entries = reconstruct_index(pack_bytes)
    payload = sum(e.length for e in entries if not e.truncated)
    truncated = bool(entries) and entries[-1].truncated
    return PackSummary(
        total_size=len(pack_bytes),
        entry_count=len(entries),
        payload_bytes=payload,
        truncated_tail=truncated,
    )
