"""Arq 5 / Arq 6 ``.pack`` and ``.index`` file parsers.

Both formats are fully published (``arq5_data_format.txt`` §"Pack
Index Format" and §"Pack File Format") and the reader implementation
in ``arq_restore/repo/PackIndex.m`` confirms the byte layout. Arq 7's
write path uses these legacy formats too whenever a pre-existing
Arq 5/6 packset is reused (``additionalUnpackedBlobDirs`` in
``backupconfig.json``); the two formats are interchangeable on the
read side.

The Arq 7 ``.pack`` format used in ``treepacks/`` / ``blobpacks/`` /
``largeblobpacks/`` is **different** and is not emitted by
arq_restore. Empirically (cross-referenced with arq_restore's
``Arq7BlobReader.m``, which reads pack-stored blobs by slicing
``BlobLoc[offset, offset+length)`` and treating the result as an
``EncryptedObject``), Arq 7 packs appear to be plain concatenations
of ``EncryptedObject``s with no per-entry framing. This module is
therefore Arq 5/6-only; for Arq 7 packs, use the standard
:class:`arq_reader.restore.Restore` workflow which honors
``BlobLoc.offset`` directly.

Index format (verified against the spec + ``PackIndex.m``):

    ``magic``     4 bytes literal ``ff 74 4f 63``
    ``version``   4 bytes BE (= 2)
    ``fanout``    256 × 4 bytes BE — cumulative count of objects whose
                  first SHA-1 byte is ≤ index. ``fanout[255]`` is the
                  total number of objects.
    ``objects``   ``fanout[255]`` × 40 bytes:
                      8 BE  offset in pack file
                      8 BE  data length
                      20    SHA-1 of the object's plaintext
                      4     padding (zero)
    ``glacier``   optional Glacier archive metadata block; absent for
                  S3 / local destinations (this module skips it if the
                  remaining bytes don't look like a SHA-1 trailer).
    ``trailer``   20 bytes — SHA-1 of every byte before the trailer.

Pack format (verified against the spec + ``PackBuilder.m``):

    ``signature`` 4 bytes literal ``50 41 43 4b`` ("PACK")
    ``version``   4 bytes BE (= 2)
    ``count``     8 bytes BE
    ``count`` × entry:
        ``[String:mimetype]``       (almost always null)
        ``[String:downloadName]``   (almost always null)
        ``[UInt64:dataLength]``
        ``dataLength`` bytes of payload
    ``trailer``   20 bytes — SHA-1 of every byte before the trailer.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import List, Tuple

PACK_INDEX_MAGIC = b"\xff\x74\x4f\x63"
PACK_FILE_MAGIC = b"PACK"          # 0x5041434b
PACK_INDEX_VERSION = 2
PACK_FILE_VERSION = 2

INDEX_FANOUT_BYTES = 256 * 4       # 1024
INDEX_OBJECT_BYTES = 8 + 8 + 20 + 4  # 40
SHA1_TRAILER_BYTES = 20


@dataclass(frozen=True)
class PackIndexEntry:
    offset: int           # byte offset within the .pack file
    data_length: int      # length of the entry's data block
    sha1_hex: str         # 40-char lowercase hex of the object SHA-1


def parse_pack_index(data: bytes) -> List[PackIndexEntry]:
    """Parse an Arq 5/6 ``.index`` file body into entries.

    Verifies the magic, version, and trailing SHA-1 (over the body).
    Returns the entries in the same order they appear (sorted by SHA-1).
    """
    if len(data) < 8 + INDEX_FANOUT_BYTES + SHA1_TRAILER_BYTES:
        raise ValueError(
            f".index too short: {len(data)} bytes"
        )
    if data[:4] != PACK_INDEX_MAGIC:
        raise ValueError(
            f"bad .index magic: {data[:4].hex()}, expected ff744f63"
        )
    version = struct.unpack(">I", data[4:8])[0]
    if version != PACK_INDEX_VERSION:
        raise ValueError(
            f"unsupported .index version: {version} (expected {PACK_INDEX_VERSION})"
        )

    fanout_off = 8
    fanout = struct.unpack(
        ">256I", data[fanout_off : fanout_off + INDEX_FANOUT_BYTES],
    )
    count = fanout[255]

    body_end = len(data) - SHA1_TRAILER_BYTES
    expected_trailer = data[body_end:]
    actual_trailer = hashlib.sha1(data[:body_end]).digest()
    if actual_trailer != expected_trailer:
        raise ValueError(
            ".index SHA-1 trailer mismatch — file is corrupted"
        )

    objects_off = fanout_off + INDEX_FANOUT_BYTES
    expected_objects_end = objects_off + count * INDEX_OBJECT_BYTES
    if expected_objects_end > body_end:
        # The Glacier-only optional block sits between objects and
        # the trailer. Local / S3 destinations don't have it; we
        # simply ignore any trailing pre-trailer bytes.
        if expected_objects_end > len(data) - SHA1_TRAILER_BYTES:
            raise ValueError(
                f"object array runs past .index body: "
                f"need {expected_objects_end - objects_off}, "
                f"have {body_end - objects_off}"
            )

    entries: List[PackIndexEntry] = []
    o = objects_off
    for _ in range(count):
        offset = struct.unpack(">Q", data[o : o + 8])[0]
        o += 8
        length = struct.unpack(">Q", data[o : o + 8])[0]
        o += 8
        sha1_bytes = data[o : o + 20]
        o += 20
        o += 4   # padding
        entries.append(PackIndexEntry(
            offset=offset, data_length=length,
            sha1_hex=sha1_bytes.hex(),
        ))
    return entries


def _read_string(data: bytes, pos: int) -> Tuple[str, int]:
    """Read an Arq ``[String]`` starting at ``pos``. Returns ``(value, new_pos)``.

    ``value`` is ``""`` for null. ``[String]`` shape:
        1 byte isNotNull (0 = null, 1 = present)
        if present: 8 bytes BE length, then UTF-8 bytes.
    """
    if pos >= len(data):
        raise ValueError(f"truncated [String] at pos={pos}")
    flag = data[pos]
    pos += 1
    if flag == 0:
        return "", pos
    if flag != 1:
        raise ValueError(f"bad [String] flag {flag} at pos={pos - 1}")
    if pos + 8 > len(data):
        raise ValueError(f"truncated [String] length at pos={pos}")
    length = struct.unpack(">Q", data[pos : pos + 8])[0]
    pos += 8
    if pos + length > len(data):
        raise ValueError(f"truncated [String] body at pos={pos}")
    s = data[pos : pos + length].decode("utf-8", errors="replace")
    return s, pos + length


@dataclass(frozen=True)
class PackEntry:
    """One entry inside an Arq 5/6 ``.pack`` file."""

    mimetype: str          # almost always empty
    download_name: str     # almost always empty
    data: bytes


def parse_pack_file(data: bytes) -> List[PackEntry]:
    """Parse an Arq 5/6 ``.pack`` file into entries.

    Verifies the PACK signature and trailing SHA-1, then iterates
    ``count`` entries reading the per-entry mime / name / length /
    data sequence.
    """
    if len(data) < 16 + SHA1_TRAILER_BYTES:
        raise ValueError(f".pack too short: {len(data)} bytes")
    if data[:4] != PACK_FILE_MAGIC:
        raise ValueError(
            f"bad .pack signature: {data[:4]!r}, expected b'PACK'"
        )
    version = struct.unpack(">I", data[4:8])[0]
    if version != PACK_FILE_VERSION:
        raise ValueError(
            f"unsupported .pack version: {version} "
            f"(expected {PACK_FILE_VERSION})"
        )
    count = struct.unpack(">Q", data[8:16])[0]

    body_end = len(data) - SHA1_TRAILER_BYTES
    expected_trailer = data[body_end:]
    actual_trailer = hashlib.sha1(data[:body_end]).digest()
    if actual_trailer != expected_trailer:
        raise ValueError(".pack SHA-1 trailer mismatch — file is corrupted")

    entries: List[PackEntry] = []
    pos = 16
    for i in range(count):
        mime, pos = _read_string(data, pos)
        name, pos = _read_string(data, pos)
        if pos + 8 > body_end:
            raise ValueError(
                f"entry {i} truncated at data-length field (pos={pos})"
            )
        dlen = struct.unpack(">Q", data[pos : pos + 8])[0]
        pos += 8
        if pos + dlen > body_end:
            raise ValueError(
                f"entry {i} data overruns body: need {dlen}, "
                f"have {body_end - pos}"
            )
        entries.append(PackEntry(
            mimetype=mime, download_name=name,
            data=data[pos : pos + dlen],
        ))
        pos += dlen
    if pos != body_end:
        raise ValueError(
            f"trailing garbage between last entry and SHA-1 trailer: "
            f"{body_end - pos} bytes"
        )
    return entries


def build_pack_index(entries: List[Tuple[str, int, int]]) -> bytes:
    """Build an Arq 5/6 ``.index`` file body.

    ``entries`` is a list of ``(sha1_hex, offset, data_length)``. The
    function sorts by SHA-1, computes the fanout, emits the binary
    layout, and appends the SHA-1 trailer. Mainly useful for tests
    + future write-side parity with Arq 5/6 backup destinations.
    """
    sorted_entries = sorted(entries, key=lambda t: t[0])
    fanout = [0] * 256
    for sha1_hex, _, _ in sorted_entries:
        first = int(sha1_hex[:2], 16)
        fanout[first] += 1
    cum = 0
    for i in range(256):
        cum += fanout[i]
        fanout[i] = cum

    out = bytearray()
    out += PACK_INDEX_MAGIC
    out += struct.pack(">I", PACK_INDEX_VERSION)
    out += struct.pack(">256I", *fanout)
    for sha1_hex, offset, length in sorted_entries:
        out += struct.pack(">Q", offset)
        out += struct.pack(">Q", length)
        out += bytes.fromhex(sha1_hex)
        out += b"\x00\x00\x00\x00"
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


def build_pack_file(entries: List[Tuple[str, int, bytes]]) -> bytes:
    """Build an Arq 5/6 ``.pack`` file body.

    ``entries`` is ``(sha1_hex, _ignored_offset, data)``. The
    ``_ignored_offset`` field is accepted (for symmetry with
    :func:`build_pack_index`) but the actual on-disk offset is
    computed during emission. Returns the full file bytes including
    SHA-1 trailer. Per the spec, mime type and download name are
    written as null strings.
    """
    out = bytearray()
    out += PACK_FILE_MAGIC
    out += struct.pack(">I", PACK_FILE_VERSION)
    out += struct.pack(">Q", len(entries))
    for _sha1_hex, _ignored_offset, data in entries:
        out += b"\x00"   # null mime type
        out += b"\x00"   # null download name
        out += struct.pack(">Q", len(data))
        out += data
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)
