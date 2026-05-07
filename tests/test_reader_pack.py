"""Pack-file-by-offset reader tests.

The Arq 7 spec lets a ``BlobLoc`` reference any byte range of any
file. The reader must be agnostic to the file's overall structure —
arq_restore's ``Arq7BlobReader.m`` confirms this by simply slicing
the ``[offset, offset+length)`` range and treating the result the
same as a standalone object.

These tests synthesize "pack files" by concatenating writer-produced
ARQOs into a single file (with optional padding to make sure offsets
matter), then drive the reader through ``BlobLoc(isPacked=True, ...)``.
"""

from __future__ import annotations

import gzip
import secrets
import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple

from arq_reader.restore import Restore
from arq_validator.backend import LocalBackend
from arq_validator.crypto import Keyset
from arq_writer.crypto_write import build_encrypted_object
from arq_writer.lz4_block import lz4_wrap
from arq_writer.types import BlobLoc


def _materialize_pack(
    dest: Path, payloads: List[bytes],
    enc_key: bytes, mac_key: bytes,
    *, padding: bytes = b"",
    compression: int = 2,
) -> Tuple[Path, List[Tuple[int, int]]]:
    """Concatenate ``payloads`` into one pack-shaped file.

    Returns ``(pack_path, [(offset, length), ...])`` so each test
    can build matching BlobLocs without re-deriving offsets.
    """
    pack = dest / "pack.dat"
    offsets: List[Tuple[int, int]] = []
    out = bytearray()
    out += padding
    for p in payloads:
        if compression == 2:
            inner = lz4_wrap(p)
        elif compression == 1:
            inner = gzip.compress(p)
        else:
            inner = p
        arqo = build_encrypted_object(inner, enc_key, mac_key)
        offsets.append((len(out), len(arqo)))
        out += arqo
        out += padding
    pack.write_bytes(bytes(out))
    return pack, offsets


class _FakeRestore(Restore):
    """Subclass that lets tests inject a synthetic Keyset directly,
    bypassing the layout/keyset discovery step (which would require
    a fully-populated backup destination)."""

    def __init__(self, src: Path, keyset: Keyset) -> None:
        # Skip the parent __init__'s keyset bookkeeping; we'll
        # supply the keyset directly via the cache.
        self.src = src.resolve()
        self.password = ""
        self.openssl_path = "openssl"
        self.backend = LocalBackend(self.src)
        self._layouts = []
        self._keyset_by_computer = {"_test_": keyset}


class PackByOffsetTests(unittest.TestCase):
    def test_three_blobs_in_one_pack(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            enc = secrets.token_bytes(32)
            mac = secrets.token_bytes(32)
            payloads = [b"alpha payload", b"beta payload " * 10, b""]
            pack, offsets = _materialize_pack(
                td, payloads, enc, mac, compression=2,
            )
            keyset = Keyset(enc, mac, b"\x00" * 32)
            r = _FakeRestore(td, keyset)

            for i, (off, length) in enumerate(offsets):
                loc = BlobLoc(
                    blobIdentifier="x" * 64,
                    isPacked=True,
                    relativePath=f"/{pack.name}",
                    offset=off,
                    length=length,
                    compressionType=2,
                )
                got = r._fetch_blob(loc, keyset)
                self.assertEqual(got, payloads[i],
                                 f"payload {i} mismatch")

    def test_padded_pack_with_random_order(self) -> None:
        # Padding between blobs + read in non-sequential order to
        # confirm the reader doesn't depend on adjacency.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            enc = secrets.token_bytes(32)
            mac = secrets.token_bytes(32)
            payloads = [secrets.token_bytes(n) for n in (16, 256, 4096, 17)]
            pack, offsets = _materialize_pack(
                td, payloads, enc, mac,
                padding=secrets.token_bytes(11),
                compression=2,
            )
            keyset = Keyset(enc, mac, b"\x00" * 32)
            r = _FakeRestore(td, keyset)

            for i in (3, 1, 0, 2):    # reverse-ish order
                off, length = offsets[i]
                loc = BlobLoc(
                    blobIdentifier="z" * 64, isPacked=True,
                    relativePath=f"/{pack.name}",
                    offset=off, length=length, compressionType=2,
                )
                self.assertEqual(r._fetch_blob(loc, keyset), payloads[i])

    def test_uncompressed_packed_blob(self) -> None:
        # compressionType=0 (no LZ4 wrap inside the ARQO).
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            enc = secrets.token_bytes(32)
            mac = secrets.token_bytes(32)
            payload = b"raw bytes, no LZ4 wrapping"
            pack, offsets = _materialize_pack(
                td, [payload], enc, mac, compression=0,
            )
            off, length = offsets[0]
            keyset = Keyset(enc, mac, b"\x00" * 32)
            loc = BlobLoc(
                blobIdentifier="0" * 64, isPacked=True,
                relativePath=f"/{pack.name}",
                offset=off, length=length, compressionType=0,
            )
            self.assertEqual(
                _FakeRestore(td, keyset)._fetch_blob(loc, keyset),
                payload,
            )

    def test_gzip_packed_blob_legacy_path(self) -> None:
        # compressionType=1 (Gzip) — Arq 5 legacy. arq_restore's
        # comment says Arq 7 doesn't emit this, but the read path
        # still has to handle reused-from-Arq-5 data.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            enc = secrets.token_bytes(32)
            mac = secrets.token_bytes(32)
            payload = b"gzip me " * 100
            pack, offsets = _materialize_pack(
                td, [payload], enc, mac, compression=1,
            )
            off, length = offsets[0]
            keyset = Keyset(enc, mac, b"\x00" * 32)
            loc = BlobLoc(
                blobIdentifier="0" * 64, isPacked=True,
                relativePath=f"/{pack.name}",
                offset=off, length=length, compressionType=1,
            )
            self.assertEqual(
                _FakeRestore(td, keyset)._fetch_blob(loc, keyset),
                payload,
            )

    def test_unencrypted_packed_blob(self) -> None:
        # arq_restore handles the case where bytes don't start with
        # ARQO — the bytes are taken as plaintext directly. Confirm.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            payload = b"unencrypted body"
            pack = td / "pack.dat"
            wrapped = lz4_wrap(payload)
            # Just write the LZ4-wrapped bytes — no ARQO around them.
            pack.write_bytes(wrapped)
            keyset = Keyset(
                secrets.token_bytes(32),
                secrets.token_bytes(32),
                b"\x00" * 32,
            )
            loc = BlobLoc(
                blobIdentifier="x" * 64, isPacked=True,
                relativePath=f"/{pack.name}",
                offset=0, length=len(wrapped), compressionType=2,
            )
            self.assertEqual(
                _FakeRestore(td, keyset)._fetch_blob(loc, keyset),
                payload,
            )


if __name__ == "__main__":
    unittest.main()
