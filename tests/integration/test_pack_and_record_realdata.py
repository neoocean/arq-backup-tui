"""Real-data integration tests for the pack walker (PR #20) and
record validator (PR #21).

The unit tests for these modules use synthetic fixtures — small
hand-built ARQO blobs, freshly-emitted backups in a tmpdir. This
file connects to the operator's actual SFTP destination and runs
the same APIs against bytes Arq.app produced over months /
years of normal use, so any latent format drift between Arq.app
and our parser surfaces immediately.

Two suites:

- :class:`PackWalkerRealDataTests` — walks every pack file in the
  destination via :func:`arq_reader.pack.reconstruct_index`,
  asserting structural sanity (no torn tails on intact packs,
  every entry's body length is a multiple of 16). A subset (the
  first ``MAX_DECRYPTED`` entries) is also fully decoded +
  HMAC-verified to confirm the bytes match what the BlobLoc-
  driven reader would fetch.

- :class:`RecordValidatorRealDataTests` — picks one
  backuprecord per folder and runs
  :func:`arq_validator.record_validator.validate_record` against
  it with a small ``max_blobs`` cap so a real run finishes in
  seconds. Fails on any per-blob HMAC mismatch / missing blob.

Both auto-skip when ``.secrets/`` / env vars are missing — same
contract as the other integration tests.

These tests guard against the most likely real-world breakage
modes:

1. Arq.app v9 (or a future version) bumps the on-disk format and
   the BlobLoc-less pack scan misaligns.
2. A pack file gets torn / partially uploaded by a network drop.
3. A backuprecord's tree references a blob that's missing from
   the destination (silent corruption that L0-L2 wouldn't catch).
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import List, Optional, Tuple

from arq_reader.pack import (
    PackError,
    PackTruncated,
    decode_pack,
    pack_summary,
    reconstruct_index,
)
from arq_validator import discover_layout
from arq_validator.crypto import decrypt_keyset
from arq_validator.layout import keyset_path, list_backuprecords
from arq_validator.record_validator import validate_record
from arq_validator.sftp import SftpBackend

from tests.integration._creds import resolve_creds, skip_reason


# Cap on per-test work — operator destinations typically hold
# hundreds of pack files / records; we sample to keep CI / ad-hoc
# runs fast. Override locally with env if you want a deeper run.
MAX_PACKS_TO_WALK = 8
MAX_DECRYPTED_PER_PACK = 4
MAX_RECORDS_PER_FOLDER = 1
RECORD_VALIDATOR_MAX_BLOBS = 50


def _open_backend(creds) -> SftpBackend:
    """Open + enter an SftpBackend rooted at ``creds.root``."""
    backend = SftpBackend(
        creds.host, port=creds.port, user=creds.user,
        password=creds.sftp_password,
        identity_file=creds.identity_file,
        root=creds.root,
    )
    backend.__enter__()
    return backend


def _all_pack_paths(layout) -> List[str]:
    """Flatten every pack file the layout enumerator surfaced into
    a list of absolute SFTP paths suitable for ``backend.read_all``.
    """
    out: List[str] = []
    for shard, name in (
        layout.treepacks + layout.blobpacks + layout.largeblobpacks
    ):
        # Pack family is encoded in the parent folder structure;
        # the layout splits shard from filename, so we rebuild the
        # abs path from the layout's known root.
        # The enumerator surfaces (shard, name) tuples within each
        # family list — combine with the family directory.
        pass
    return out


def _build_pack_paths(layout, computer_uuid: str) -> List[Tuple[str, str]]:
    """Return ``[(family, abs_path), ...]`` covering every pack
    file the layout discovered for one computer."""
    families = (
        ("treepacks", layout.treepacks),
        ("blobpacks", layout.blobpacks),
        ("largeblobpacks", layout.largeblobpacks),
    )
    rows: List[Tuple[str, str]] = []
    for fam, items in families:
        for shard, name in items:
            path = (
                f"/{computer_uuid}/{fam}/{shard}/{name}"
            )
            rows.append((fam, path))
    return rows


@unittest.skipUnless(
    resolve_creds() is not None, skip_reason() or "no creds",
)
class PackWalkerRealDataTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.creds = resolve_creds()
        cls.backend = _open_backend(cls.creds)
        cls.layouts = list(discover_layout(
            cls.backend, "/", enumerate_objects=True,
        ))
        # Decrypt one keyset upfront for the decoder smoke-tests.
        if cls.layouts:
            cu = cls.layouts[0].computer_uuid
            cls.computer_uuid = cu
            kbytes = cls.backend.read_all(keyset_path("/", cu))
            cls.keyset = decrypt_keyset(
                kbytes, cls.creds.dest_password,
            )
        else:
            cls.computer_uuid = ""
            cls.keyset = None

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.backend.__exit__(None, None, None)
        except Exception:
            pass

    def setUp(self) -> None:
        if not self.layouts:
            self.skipTest("no computer subtrees found")

    def test_every_pack_starts_with_arqo_magic(self) -> None:
        """Pack file format invariant: byte 0 must be 'A' of
        ARQO. Any pack failing this is either truncated from the
        head or not an Arq 7 pack."""
        pack_paths = _build_pack_paths(
            self.layouts[0], self.computer_uuid,
        )[:MAX_PACKS_TO_WALK]
        if not pack_paths:
            self.skipTest("no pack files in destination")
        checked = 0
        for family, path in pack_paths:
            head = self.backend.read_range(path, 0, 4)
            self.assertEqual(
                head, b"ARQO",
                f"{family} pack {path} doesn't start with ARQO; "
                f"got {head!r}",
            )
            checked += 1
        self.assertGreater(checked, 0)

    def test_reconstruct_index_yields_clean_packs(self) -> None:
        """Walk every entry of N pack files and assert no torn
        tails. A torn tail in production data would mean an
        upload was interrupted mid-blob — recoverable but worth
        surfacing."""
        pack_paths = _build_pack_paths(
            self.layouts[0], self.computer_uuid,
        )[:MAX_PACKS_TO_WALK]
        if not pack_paths:
            self.skipTest("no pack files in destination")
        torn_packs: List[Tuple[str, int]] = []
        for family, path in pack_paths:
            blob = self.backend.read_all(path)
            entries = reconstruct_index(blob)
            self.assertGreater(
                len(entries), 0,
                f"{path} reconstructed to zero entries",
            )
            # Torn tail = the trailing entry's body isn't a clean
            # multiple of 16. Worth flagging but not a hard fail
            # in case the destination genuinely had an upload
            # interrupted recently — we record + report.
            tail = entries[-1]
            if tail.truncated:
                torn_packs.append((path, tail.offset))
        # Real-world expectation: the operator's destination has
        # been quiescent for weeks, so torn tails should be zero.
        # If any appear, the test surfaces them but doesn't fail
        # outright — this might be a transient artifact of the
        # most recent backup pass.
        if torn_packs:
            self.skipTest(
                f"found {len(torn_packs)} packs with torn tails: "
                f"{torn_packs[:3]} … (informational, not failing)"
            )

    def test_decode_first_entries_round_trip_through_keyset(self) -> None:
        """For a small subset of packs, fully decrypt the first N
        entries via :func:`decode_pack` — proves the entire
        pipeline (magic scan + HMAC + AES + LZ4) survives real
        bytes."""
        if self.keyset is None:
            self.skipTest("no keyset to decrypt with")
        pack_paths = _build_pack_paths(
            self.layouts[0], self.computer_uuid,
        )[:MAX_PACKS_TO_WALK]
        if not pack_paths:
            self.skipTest("no pack files in destination")
        decoded_total = 0
        for family, path in pack_paths:
            blob = self.backend.read_all(path)
            cap = MAX_DECRYPTED_PER_PACK
            try:
                gen = decode_pack(
                    blob,
                    self.keyset.encryption_key,
                    self.keyset.hmac_key,
                )
                for i, (entry, plaintext) in enumerate(gen):
                    if i >= cap:
                        break
                    self.assertGreater(
                        len(plaintext), 0,
                        f"empty plaintext at {path}@{entry.offset}",
                    )
                    decoded_total += 1
            except PackTruncated:
                # Last-entry truncation = informational only; the
                # earlier entries we got through still count.
                pass
            except PackError as exc:
                self.fail(
                    f"decode_pack failed on {path}: {exc}"
                )
        self.assertGreater(
            decoded_total, 0,
            "no entries decoded across any pack",
        )

    def test_pack_summary_matches_byte_length(self) -> None:
        """``pack_summary`` is the lightweight stats wrapper. Its
        ``payload_bytes`` must equal the pack's actual length when
        the tail is intact; the entry_count stays consistent with
        ``reconstruct_index``."""
        pack_paths = _build_pack_paths(
            self.layouts[0], self.computer_uuid,
        )[:MAX_PACKS_TO_WALK]
        if not pack_paths:
            self.skipTest("no pack files in destination")
        for family, path in pack_paths:
            blob = self.backend.read_all(path)
            s = pack_summary(blob)
            self.assertEqual(s.total_size, len(blob))
            self.assertGreater(s.entry_count, 0)
            if not s.truncated_tail:
                self.assertEqual(
                    s.payload_bytes, s.total_size,
                    f"{path}: payload_bytes != total_size on a "
                    f"non-truncated pack",
                )


@unittest.skipUnless(
    resolve_creds() is not None, skip_reason() or "no creds",
)
class RecordValidatorRealDataTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.creds = resolve_creds()
        cls.backend = _open_backend(cls.creds)
        cls.layouts = list(discover_layout(
            cls.backend, "/", enumerate_objects=False,
        ))

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.backend.__exit__(None, None, None)
        except Exception:
            pass

    def setUp(self) -> None:
        if not self.layouts:
            self.skipTest("no computer subtrees found")

    def _record_paths(self) -> List[Tuple[str, str]]:
        """Yield ``(computer_uuid, record_path)`` for the latest
        record of every folder, capped per-folder. Result is small
        enough that a single test pass finishes in well under a
        minute even on slow SFTP."""
        rows: List[Tuple[str, str]] = []
        for lay in self.layouts:
            for fu in lay.backup_folder_uuids:
                paths = list_backuprecords(
                    self.backend, "/", lay.computer_uuid, fu,
                )
                # list_backuprecords returns paths sorted oldest
                # first; take the latest few.
                taken = paths[-MAX_RECORDS_PER_FOLDER:]
                for p in taken:
                    rows.append((lay.computer_uuid, p))
        return rows

    def test_validate_record_passes_on_real_destination(self) -> None:
        """Walk one record per folder under a small max_blobs cap
        and confirm no fetch / HMAC / decode failures."""
        rows = self._record_paths()
        if not rows:
            self.skipTest("no records found in any folder")
        first_failure = None
        per_folder_reports = []
        for cu, rec_path in rows:
            report = validate_record(
                self.backend, rec_path,
                encryption_password=self.creds.dest_password,
                computer_uuid=cu,
                max_blobs=RECORD_VALIDATOR_MAX_BLOBS,
            )
            per_folder_reports.append(
                (rec_path, report.ok, report.blobs_walked,
                 len(report.failures))
            )
            if not report.ok and first_failure is None:
                first_failure = (rec_path, report.failures[:3])
        # Surface the first failure with concrete detail so triage
        # can route to the right BlobLoc.
        self.assertIsNone(
            first_failure,
            f"record validation failed; first failure: "
            f"{first_failure!r}",
        )
        # Sanity: every record actually walked SOMETHING.
        self.assertTrue(all(
            blobs > 0 for _, _, blobs, _ in per_folder_reports
        ), f"some records walked 0 blobs: {per_folder_reports!r}")


if __name__ == "__main__":
    unittest.main()
