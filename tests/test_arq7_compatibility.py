"""Comprehensive Arq 7 on-disk format compatibility tests.

Each test builds a backup with ``arq_writer.build_backup`` against
varied source shapes (empty / single file / multi-folder / unicode
/ large / chunked / packed vs standalone) and runs every invariant
in :mod:`arq_validator.compatibility` against the result.

The full suite is the answer to "is what we write actually
Arq-7-shaped?" -- if every check here passes for every scenario,
the destination matches the documented format byte-for-byte
modulo the deliberate scope omissions noted in
``docs/COMPATIBILITY.md`` (no largeblobpacks/ routing, no
unencrypted mode).
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import unittest
from pathlib import Path

from arq_validator import (
    CheckResult,
    ComplianceReport,
    LocalBackend,
    check_arq7_compatibility,
)
from arq_writer import Backup, build_backup
from arq_writer.backuprecord import parse_backuprecord as _parse_record


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _print_failures(report: ComplianceReport) -> str:
    """Format the failed checks for assertion-failure messages."""
    if report.passed:
        return "(no failures)"
    return "\n".join(
        f"  [{c.id}] {c.name}: {c.message}"
        for c in report.failed_checks
    )


def _assert_passes(testcase: unittest.TestCase, report: ComplianceReport) -> None:
    testcase.assertTrue(
        report.passed,
        msg=f"Compatibility report failed:\n{_print_failures(report)}\n"
        f"summary: {report.summary()}",
    )


def _make_simple_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "beta.txt").write_bytes(b"beta\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma\n")


def _read_sidecar_dict(path: Path, dest: Path, cu: str, password: str) -> dict:
    """Read a sidecar JSON file, transparently decrypting ARQO
    envelopes via the destination's keyset. Mirrors what
    ``arq_validator.sidecar.read_sidecar`` does, but inlined here so
    the tests aren't testing a helper with the helper they're
    asserting against."""
    from arq_reader.decrypt import decrypt_encrypted_object
    from arq_validator.crypto import decrypt_keyset

    raw = path.read_bytes()
    if raw[:4] == b"ARQO":
        keyset = decrypt_keyset(
            (dest / cu / "encryptedkeyset.dat").read_bytes(), password,
        )
        plain = decrypt_encrypted_object(
            raw, keyset.encryption_key, keyset.hmac_key,
        )
    else:
        plain = raw
    return json.loads(plain.decode("utf-8"))


def _mutate_encrypted_sidecar(
    path: Path, dest: Path, cu: str, password: str, mutator,
) -> None:
    """Decrypt → mutate (caller-supplied) → re-encrypt → write back.
    Required for tests that perturb a sidecar after a build_backup
    call, since post-T1 the sidecars on disk are ARQO-wrapped."""
    from arq_reader.decrypt import decrypt_encrypted_object
    from arq_validator.crypto import decrypt_keyset
    from arq_writer.crypto_write import build_encrypted_object

    keyset = decrypt_keyset(
        (dest / cu / "encryptedkeyset.dat").read_bytes(), password,
    )
    raw = path.read_bytes()
    is_encrypted = raw[:4] == b"ARQO"
    plain = (
        decrypt_encrypted_object(
            raw, keyset.encryption_key, keyset.hmac_key,
        ) if is_encrypted else raw
    )
    data = json.loads(plain.decode("utf-8"))
    data = mutator(data)
    new_plain = json.dumps(
        data, indent=2, ensure_ascii=False,
    ).encode("utf-8")
    if is_encrypted:
        new_raw = build_encrypted_object(
            new_plain, keyset.encryption_key, keyset.hmac_key,
        )
    else:
        new_raw = new_plain
    path.write_bytes(new_raw)


# ---------------------------------------------------------------------------
# Scenario coverage matrix
# ---------------------------------------------------------------------------


class StandaloneSimpleTree(unittest.TestCase):
    def test_simple_tree_in_standalone_mode_passes_all_invariants(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(
                src, dest, encryption_password="pw",
                use_packs=False,
            )
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            _assert_passes(self, report)
            self.assertEqual(report.computer_uuid, r.computer_uuid)
            self.assertIn(r.folder_uuid, report.folder_uuids)


class PackedSimpleTree(unittest.TestCase):
    def test_simple_tree_in_packed_mode_passes_all_invariants(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(
                src, dest, encryption_password="pw",
                use_packs=True,
            )
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            _assert_passes(self, report)


class EmptySource(unittest.TestCase):
    def test_empty_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            _assert_passes(self, report)


class SingleFile(unittest.TestCase):
    def test_single_root_file_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "only.txt").write_bytes(b"only file\n")
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            _assert_passes(self, report)


class UnicodePathsCompatibility(unittest.TestCase):
    def test_korean_japanese_emoji_filenames_round_trip(self) -> None:
        # Arq 7 stores filenames as UTF-8 inside Tree blobs; the
        # compatibility checker must accept any UTF-8 sequence.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "한글폴더").mkdir()
            (src / "한글폴더" / "메모.txt").write_bytes(
                "한국어 내용".encode("utf-8")
            )
            (src / "写真").mkdir()
            (src / "写真" / "東京タワー.jpg").write_bytes(b"\xff\xd8placeholder")
            (src / "🎵").mkdir()
            (src / "🎵" / "song.mp3").write_bytes(b"id3 stub")
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            _assert_passes(self, report)


class MultiFolder(unittest.TestCase):
    def test_two_folders_in_one_computer_pass_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src_a = tdp / "src_a"
            src_a.mkdir()
            (src_a / "a.txt").write_bytes(b"A\n")
            src_b = tdp / "src_b"
            src_b.mkdir()
            (src_b / "b.txt").write_bytes(b"B\n")
            dest = tdp / "dest"
            bk = Backup(
                dest_root=dest, encryption_password="pw",
                use_packs=True,
            )
            bk.init_plan()
            bk.add_folder(src_a, folder_name="A")
            bk.add_folder(src_b, folder_name="B")
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=bk.computer_uuid,
            )
            _assert_passes(self, report)
            # Both folder UUIDs must show up in the report.
            self.assertEqual(len(report.folder_uuids), 2)


class LargeFileChunked(unittest.TestCase):
    def test_chunked_large_file_with_arq_v7_params_passes(self) -> None:
        # Stress the chunker with the Arq.app v7.41 parameters and
        # a >2 MiB file so multiple chunks are emitted.
        from arq_writer.arq_chunker_params import ARQ_V7_CHUNKER_CONFIG

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "big.bin").write_bytes(secrets.token_bytes(3 * 1024 * 1024))
            dest = tdp / "dest"
            r = build_backup(
                src, dest, encryption_password="pw",
                use_packs=True,
                chunker_config=ARQ_V7_CHUNKER_CONFIG,
            )
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            _assert_passes(self, report)


# ---------------------------------------------------------------------------
# Targeted negative tests — corrupt one byte and confirm the right
# invariant flags it. Catches regressions where the checker silently
# accepts a malformed destination.
# ---------------------------------------------------------------------------


class CheckerCatchesCorruption(unittest.TestCase):
    def test_corrupt_keyset_magic_fails_C1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            keyset_path = (
                dest / r.computer_uuid / "encryptedkeyset.dat"
            )
            data = bytearray(keyset_path.read_bytes())
            data[0] = (data[0] + 1) % 256       # corrupt magic
            keyset_path.write_bytes(bytes(data))
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            self.assertFalse(report.passed)
            ids = {c.id for c in report.failed_checks}
            self.assertIn("C1", ids)

    def test_wrong_password_fails_C3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="WRONG",
                computer_uuid=r.computer_uuid,
            )
            self.assertFalse(report.passed)
            ids = {c.id for c in report.failed_checks}
            self.assertIn("C3", ids)

    def test_corrupt_arqo_magic_fails_A1(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(
                src, dest, encryption_password="pw",
                use_packs=False,
            )
            so_root = dest / r.computer_uuid / "standardobjects"
            # Walk to the first standardobject and corrupt its
            # ARQO magic.
            corrupted = False
            for shard in os.scandir(so_root):
                if not shard.is_dir():
                    continue
                for f in os.scandir(shard.path):
                    if f.is_file():
                        data = bytearray(Path(f.path).read_bytes())
                        data[:4] = b"XXXX"
                        Path(f.path).write_bytes(bytes(data))
                        corrupted = True
                        break
                if corrupted:
                    break
            self.assertTrue(corrupted)
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            self.assertFalse(report.passed)
            ids = {c.id for c in report.failed_checks}
            self.assertIn("A1", ids)

    def test_missing_backupconfig_fails_L3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            (dest / r.computer_uuid / "backupconfig.json").unlink()
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            self.assertFalse(report.passed)
            ids = {c.id for c in report.failed_checks}
            self.assertIn("L3", ids)

    def test_corrupted_backupplan_field_fails_L4(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            # backupplan.json is ARQO-encrypted (T1) — round-trip
            # the field deletion through decrypt → mutate → encrypt
            # so the L4 audit decrypts successfully and then sees
            # the missing planUUID, not an opaque ARQO parse error.
            plan_path = dest / r.computer_uuid / "backupplan.json"

            def _drop_plan_uuid(plan: dict) -> dict:
                del plan["planUUID"]
                return plan

            _mutate_encrypted_sidecar(
                plan_path, dest, r.computer_uuid, "pw",
                _drop_plan_uuid,
            )
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            self.assertFalse(report.passed)
            failed_names = {c.name for c in report.failed_checks}
            self.assertTrue(any(
                "planUUID" in n for n in failed_names
            ))


# ---------------------------------------------------------------------------
# Spec-level field invariants -- verify the fixed values our writer
# emits in JSON sidecars match the values Arq.app actually expects.
# These are tighter than the type-only checks in the main checker.
# ---------------------------------------------------------------------------


class SpecLevelFieldValues(unittest.TestCase):
    def test_fixed_values_in_backupconfig_match_spec(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            cfg = json.loads(
                (dest / r.computer_uuid / "backupconfig.json").read_text()
            )
            # Spec / observed values:
            self.assertEqual(cfg["chunkerVersion"], 3)
            self.assertEqual(cfg["blobIdentifierType"], 2)   # SHA-256
            self.assertEqual(cfg["isEncrypted"], True)
            self.assertEqual(cfg["isWORM"], False)
            self.assertEqual(cfg["containsGlacierArchives"], False)
            self.assertEqual(cfg["additionalUnpackedBlobDirs"], [])
            self.assertEqual(cfg["blobStorageClass"], "STANDARD")
            self.assertEqual(cfg["maxPackedItemLength"], 256000)

    def test_fixed_values_in_backupplan_match_spec(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            # backupplan.json is ARQO-encrypted post-T1 — read via
            # the keyset so we can inspect the structured values.
            plan = _read_sidecar_dict(
                dest / r.computer_uuid / "backupplan.json",
                dest, r.computer_uuid, "pw",
            )
            self.assertEqual(plan["version"], 2)
            self.assertEqual(plan["isEncrypted"], True)
            self.assertEqual(plan["active"], True)
            # backupFolderPlansByUUID must have at least one entry
            # after a successful backup.
            self.assertGreaterEqual(len(plan["backupFolderPlansByUUID"]), 1)
            for fp in plan["backupFolderPlansByUUID"].values():
                self.assertIn("backupFolderUUID", fp)
                self.assertIn("localPath", fp)
                self.assertIn("name", fp)

    def test_backuprecord_version_is_100(self) -> None:
        # Decrypt the latest backuprecord and verify the top-level
        # "version" key matches the writer constant.
        import plistlib

        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_validator.crypto import decrypt_keyset

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_simple_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            keyset = decrypt_keyset(
                (dest / r.computer_uuid / "encryptedkeyset.dat").read_bytes(),
                "pw",
            )
            arqo = Path(r.backuprecord_path).read_bytes()
            plist = _parse_record(decrypt_lz4_arqo(
                arqo, keyset.encryption_key, keyset.hmac_key,
            ))
            self.assertEqual(plist["version"], 100)
            self.assertEqual(plist["isComplete"], True)
            self.assertIn("node", plist)
            self.assertIn("creationDate", plist)


if __name__ == "__main__":
    unittest.main()
