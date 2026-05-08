"""Deep compatibility verification against the operator's real
Arq.app SFTP destination.

Companion to ``test_arq_real_destination.py`` (which exercises the
runtime behaviour of reader/validator/writer in isolation). This
module targets **format compatibility** — the precise byte layouts
and key inventories Arq.app actually produces vs. what our
reader/writer assume. Two compatibility bugs (JSON backuprecord +
``isLargePack`` binary field) were discovered while bringing this
suite up; the tests below are designed to catch the *next* one
before it bites a real round-trip.

Tests are read-only against the operator's real backups (no
sandbox writes), so they're safe to re-run.

Auto-skipped when ``.secrets/`` / ``.env`` / env vars don't
provide credentials.
"""

from __future__ import annotations

import json
import re
import unittest
from typing import Any, Dict, Set

from arq_reader.decrypt import decrypt_lz4_arqo
from arq_reader.parse import parse_tree
from arq_reader.restore import Restore, _parse_backuprecord
from arq_validator import discover_layout
from arq_validator.crypto import decrypt_keyset
from arq_validator.layout import (
    find_latest_backuprecord,
    list_backuprecords,
)
from arq_validator.sftp import SftpBackend
from arq_writer.types import BlobLoc, FileNode, TreeNode

from tests.integration._creds import resolve_creds, skip_reason


# ---------------------------------------------------------------------------
# Test fixture: open one shared backend per test class so we don't pay
# the SSH master setup cost N times.
# ---------------------------------------------------------------------------


def _open_backend(creds):
    backend = SftpBackend(
        host=creds.host, user=creds.user, port=creds.port,
        password=creds.sftp_password,
        identity_file=creds.identity_file,
        root=creds.root,
    )
    backend.__enter__()
    return backend


@unittest.skipUnless(
    resolve_creds() is not None,
    skip_reason() or "no credentials",
)
class _RealDestinationBase(unittest.TestCase):
    """Shared setup: resolve creds + open backend + decrypt keyset
    once per class."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.creds = resolve_creds()
        cls.backend = _open_backend(cls.creds)
        cls.layouts = discover_layout(
            cls.backend, "/", enumerate_objects=False,
        )
        if not cls.layouts:
            cls.tearDownClass()
            raise unittest.SkipTest("no computer UUIDs at root")
        cls.cu = cls.layouts[0].computer_uuid
        keyset_blob = cls.backend.read_all(
            f"/{cls.cu}/encryptedkeyset.dat",
        )
        cls.keyset = decrypt_keyset(
            keyset_blob, cls.creds.dest_password,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        be = getattr(cls, "backend", None)
        if be is not None:
            be.close()


# ---------------------------------------------------------------------------
# Priority 1 — every folder + historical records parse cleanly
# ---------------------------------------------------------------------------


class FolderAndHistoryParseTests(_RealDestinationBase):
    """Priority 1: format invariant across folders + chronology.

    If Arq changed the backuprecord JSON layout between versions,
    or one folder uses an exotic shape, these tests surface it as
    a clean failure instead of the generic ``KeyError`` /
    ``ValueError`` the rest of the reader emits."""

    def test_every_folder_has_decryptable_latest_record(self) -> None:
        layout = self.layouts[0]
        self.assertGreaterEqual(
            len(layout.backup_folder_uuids), 1,
            "no backup folders found",
        )
        for fu in layout.backup_folder_uuids:
            with self.subTest(folder=fu):
                rec_path = find_latest_backuprecord(
                    self.backend, "/", self.cu, fu,
                )
                self.assertIsNotNone(
                    rec_path,
                    f"no backuprecord for folder {fu}",
                )
                arqo = self.backend.read_all(rec_path)
                plain = decrypt_lz4_arqo(
                    arqo,
                    self.keyset.encryption_key,
                    self.keyset.hmac_key,
                )
                rec = _parse_backuprecord(plain)
                # Every spec-required top-level key must be present;
                # this is what arq_restore + Arq.app both consult.
                for key in (
                    "arqVersion", "backupFolderUUID", "backupPlanUUID",
                    "creationDate", "isComplete", "node", "version",
                ):
                    self.assertIn(
                        key, rec,
                        f"folder {fu}: backuprecord missing key {key!r}",
                    )

    def test_oldest_record_still_decrypts(self) -> None:
        """If Arq rotated keysets / changed format mid-history,
        the oldest record would fail to decrypt. Surface it."""
        layout = self.layouts[0]
        for fu in layout.backup_folder_uuids:
            recs = list_backuprecords(
                self.backend, "/", self.cu, fu,
            )
            if not recs:
                continue
            oldest = recs[0]   # list_backuprecords returns oldest-first
            with self.subTest(folder=fu, record=oldest):
                arqo = self.backend.read_all(oldest)
                plain = decrypt_lz4_arqo(
                    arqo,
                    self.keyset.encryption_key,
                    self.keyset.hmac_key,
                )
                rec = _parse_backuprecord(plain)
                self.assertIn("node", rec)

    def test_record_paths_sort_chronologically(self) -> None:
        """``list_backuprecords`` returns paths in lexicographic
        order; the reader relies on that being chronological order
        of ``creationDate``. We deliberately don't assert the exact
        ``bucket = f(creationDate)`` formula — empirical numbers
        from a real Hetzner destination disagreed with the
        ``floor(creationDate / 100000)`` form our spec doc claimed
        (real ratio is closer to ``/ 10_000_000``, with ``num``
        unrelated to ``creationDate`` modulo). The chronological
        ordering invariant is what the reader actually consumes;
        the bucket internals are Arq.app's to define.
        """
        layout = self.layouts[0]
        for fu in layout.backup_folder_uuids:
            recs = list_backuprecords(self.backend, "/", self.cu, fu)
            if len(recs) < 2:
                continue
            with self.subTest(folder=fu):
                creation_dates = []
                for path in recs[:5]:  # sample first 5 to bound time
                    arqo = self.backend.read_all(path)
                    plain = decrypt_lz4_arqo(
                        arqo,
                        self.keyset.encryption_key,
                        self.keyset.hmac_key,
                    )
                    rec = _parse_backuprecord(plain)
                    cdate = rec.get("creationDate")
                    self.assertIsNotNone(
                        cdate,
                        f"record {path} missing creationDate",
                    )
                    creation_dates.append(float(cdate))
                self.assertEqual(
                    creation_dates, sorted(creation_dates),
                    "list_backuprecords didn't return chronological "
                    "order — reader depends on this for "
                    "find_latest_backuprecord",
                )


# ---------------------------------------------------------------------------
# Priority 2 — every Tree blob parses cleanly
# ---------------------------------------------------------------------------


def _walk_tree(rs: Restore, tree_blob_loc: BlobLoc, keyset, *,
               trees_seen: Set[str], cap: int) -> int:
    """Recursive tree walk that collects + parses every Tree blob
    reachable from ``tree_blob_loc``. Returns count of trees parsed.
    Caps recursion at ``cap`` to keep tests bounded on huge
    destinations."""
    if tree_blob_loc.blobIdentifier in trees_seen:
        return 0
    trees_seen.add(tree_blob_loc.blobIdentifier)
    tree_bytes = rs._fetch_blob(tree_blob_loc, keyset)
    tree = parse_tree(tree_bytes)
    count = 1
    for child in tree.children:
        if count >= cap:
            break
        if isinstance(child.node, TreeNode):
            count += _walk_tree(
                rs, child.node.treeBlobLoc, keyset,
                trees_seen=trees_seen, cap=cap - count,
            )
    return count


class TreeBinaryParseTests(_RealDestinationBase):
    """Priority 2: every Arq.app-produced Tree blob parses cleanly.

    The ``isLargePack`` field discovery happened against the very
    first tree we tried; this test makes sure no other binary
    layout drift hides further down the tree (e.g. Tree v4-only
    fields, nested xattrs, ACL blobs)."""

    def test_top_level_tree_parses(self) -> None:
        """Just the root tree of the latest record per folder."""
        rs = Restore(
            "/", encryption_password=self.creds.dest_password,
            backend=self.backend,
        )
        layout = self.layouts[0]
        for fu in layout.backup_folder_uuids:
            with self.subTest(folder=fu):
                rec_path = find_latest_backuprecord(
                    self.backend, "/", self.cu, fu,
                )
                arqo = self.backend.read_all(rec_path)
                plain = decrypt_lz4_arqo(
                    arqo,
                    self.keyset.encryption_key,
                    self.keyset.hmac_key,
                )
                rec = _parse_backuprecord(plain)
                node = rec["node"]
                if not node.get("isTree"):
                    continue
                tbl = rs._blobloc_from_dict(node["treeBlobLoc"])
                tree_bytes = rs._fetch_blob(tbl, self.keyset)
                tree = parse_tree(tree_bytes)
                self.assertGreater(
                    tree.version, 0,
                    f"folder {fu}: tree version is zero",
                )

    def test_nested_trees_parse_cleanly(self) -> None:
        """Walk up to ``MAX_TREES`` Tree blobs from each folder's
        root; every blob must decrypt + parse without raising."""
        MAX_TREES = 50  # cap so the test stays bounded
        rs = Restore(
            "/", encryption_password=self.creds.dest_password,
            backend=self.backend,
        )
        layout = self.layouts[0]
        # Just first folder — N folders × 50 trees would be slow on
        # SFTP; this is a sample, not exhaustive.
        if not layout.backup_folder_uuids:
            self.skipTest("no folders")
        fu = layout.backup_folder_uuids[0]
        rec_path = find_latest_backuprecord(
            self.backend, "/", self.cu, fu,
        )
        arqo = self.backend.read_all(rec_path)
        plain = decrypt_lz4_arqo(
            arqo,
            self.keyset.encryption_key,
            self.keyset.hmac_key,
        )
        rec = _parse_backuprecord(plain)
        node = rec["node"]
        if not node.get("isTree"):
            self.skipTest("root is not a tree")
        tbl = rs._blobloc_from_dict(node["treeBlobLoc"])
        seen: Set[str] = set()
        n = _walk_tree(
            rs, tbl, self.keyset,
            trees_seen=seen, cap=MAX_TREES,
        )
        self.assertGreaterEqual(
            n, 1, f"folder {fu}: walked zero trees",
        )

    def test_every_folder_root_tree_parses(self) -> None:
        """Each folder's *root* Tree blob (across both v3 + v4
        formats) parses without raising. Catches any per-version
        binary-layout drift the fix for v4's 38-byte trailing
        block didn't anticipate. Stays bounded by reading only
        the root tree (one pack file per folder)."""
        from arq_reader.parse import parse_tree
        rs = Restore(
            "/", encryption_password=self.creds.dest_password,
            backend=self.backend,
        )
        layout = self.layouts[0]
        if not layout.backup_folder_uuids:
            self.skipTest("no folders")
        for fu in layout.backup_folder_uuids:
            with self.subTest(folder=fu):
                rec_path = find_latest_backuprecord(
                    self.backend, "/", self.cu, fu,
                )
                arqo = self.backend.read_all(rec_path)
                plain = decrypt_lz4_arqo(
                    arqo, self.keyset.encryption_key,
                    self.keyset.hmac_key,
                )
                rec = _parse_backuprecord(plain)
                node = rec["node"]
                if not node.get("isTree"):
                    continue
                tbl = rs._blobloc_from_dict(node["treeBlobLoc"])
                tree_bytes = rs._fetch_blob(tbl, self.keyset)
                version = int.from_bytes(tree_bytes[:4], "big")
                tree = parse_tree(tree_bytes)
                # Empty trees are unusual but legal; any decode
                # failure is the actual signal.
                self.assertGreaterEqual(
                    len(tree.children), 0,
                    f"folder {fu}: v={version} tree decode failed",
                )


# ---------------------------------------------------------------------------
# Priority 3 — writer's JSON output shape vs. Arq.app's
# ---------------------------------------------------------------------------


class WriterFormatCompatTests(_RealDestinationBase):
    """Priority 3: assert the JSON keys our writer emits cover at
    least the same set Arq.app emits.

    A real-world case: Arq.app adds a new key in v8 → our writer
    is silently behind. This test surfaces it."""

    def test_node_keys_overlap_with_arq_app(self) -> None:
        """For each folder's latest record, collect the operator's
        ``node`` dict keys and assert our writer's
        ``node_to_dict`` would emit at least those keys (modulo
        ones we deliberately omit, like ``copiedFrom*``)."""
        from arq_writer.backuprecord import node_to_dict
        from arq_writer.types import FileNode, TreeNode, BlobLoc

        # Build a representative node via our writer's helper so
        # we can compare key inventories.
        synthetic = TreeNode(
            treeBlobLoc=BlobLoc(blobIdentifier="0" * 64),
        )
        our_keys = set(node_to_dict(synthetic).keys())

        # Keys we deliberately don't emit (handled by parent dict
        # in Arq.app vs ours, or version-specific extensions we
        # haven't adopted).
        TOLERATED_GAPS = {
            "reparseTag", "reparsePointIsDirectory", "winAttrs",
            # macOS-specific fields not needed for cross-platform:
            "mac_st_dev", "mac_st_ino", "mac_st_nlink",
            "mac_st_rdev", "mac_st_flags",
        }

        layout = self.layouts[0]
        for fu in layout.backup_folder_uuids[:1]:  # just first folder
            with self.subTest(folder=fu):
                rec_path = find_latest_backuprecord(
                    self.backend, "/", self.cu, fu,
                )
                arqo = self.backend.read_all(rec_path)
                plain = decrypt_lz4_arqo(
                    arqo,
                    self.keyset.encryption_key,
                    self.keyset.hmac_key,
                )
                rec = _parse_backuprecord(plain)
                arq_node_keys = set(rec["node"].keys())
                missing = arq_node_keys - our_keys - TOLERATED_GAPS
                self.assertEqual(
                    missing, set(),
                    f"folder {fu}: writer's node_to_dict missing "
                    f"keys Arq.app emits: {sorted(missing)}",
                )

    def test_record_top_level_keys_overlap(self) -> None:
        """Backuprecord top-level dict comparison — same idea as
        node, but for the outer envelope."""
        from arq_writer.backuprecord import build_backuprecord_dict
        from arq_writer.types import TreeNode, BlobLoc
        # Build one synthetic record so we know our key set.
        synth_node = TreeNode(
            treeBlobLoc=BlobLoc(blobIdentifier="0" * 64),
        )
        our_record = build_backuprecord_dict(
            backup_folder_uuid="A" * 36,
            backup_plan_uuid="B" * 36,
            backup_plan_dict={},
            root_node=synth_node,
            local_path="/x",
        )
        our_keys = set(our_record.keys())

        # Operator-side keys we deliberately don't emit (Arq.app-
        # specific UI metadata that has no functional role for
        # restore — copiedFrom history, disk identifiers, etc.).
        TOLERATED_GAPS = {
            "backupRecordErrors", "copiedFromCommit",
            "copiedFromSnapshot", "diskIdentifier",
            "localMountPoint", "volumeName",
        }

        layout = self.layouts[0]
        if not layout.backup_folder_uuids:
            self.skipTest("no folders")
        fu = layout.backup_folder_uuids[0]
        rec_path = find_latest_backuprecord(
            self.backend, "/", self.cu, fu,
        )
        arqo = self.backend.read_all(rec_path)
        plain = decrypt_lz4_arqo(
            arqo, self.keyset.encryption_key,
            self.keyset.hmac_key,
        )
        rec = _parse_backuprecord(plain)
        arq_keys = set(rec.keys())
        missing = arq_keys - our_keys - TOLERATED_GAPS
        self.assertEqual(
            missing, set(),
            f"writer's build_backuprecord_dict missing keys "
            f"Arq.app emits: {sorted(missing)}",
        )

    def test_blobloc_keys_overlap(self) -> None:
        """The ``isLargePack`` discovery was a missing BlobLoc
        key. Lock it in: every key Arq.app emits in a treeBlobLoc
        must appear in our ``blobloc_to_dict`` output."""
        from arq_writer.backuprecord import blobloc_to_dict
        from arq_writer.types import BlobLoc
        our_keys = set(
            blobloc_to_dict(BlobLoc(blobIdentifier="0" * 64)).keys(),
        )

        layout = self.layouts[0]
        if not layout.backup_folder_uuids:
            self.skipTest("no folders")
        fu = layout.backup_folder_uuids[0]
        rec_path = find_latest_backuprecord(
            self.backend, "/", self.cu, fu,
        )
        arqo = self.backend.read_all(rec_path)
        plain = decrypt_lz4_arqo(
            arqo, self.keyset.encryption_key,
            self.keyset.hmac_key,
        )
        rec = _parse_backuprecord(plain)
        node = rec["node"]
        if not node.get("isTree"):
            self.skipTest("root not a tree")
        arq_keys = set(node["treeBlobLoc"].keys())
        missing = arq_keys - our_keys
        self.assertEqual(
            missing, set(),
            f"writer's blobloc_to_dict missing keys: "
            f"{sorted(missing)}",
        )


if __name__ == "__main__":
    unittest.main()
