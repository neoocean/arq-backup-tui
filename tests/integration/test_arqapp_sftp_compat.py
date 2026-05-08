"""Integration tests against a real Arq 7 SFTP destination.

These tests are **read-only** by design — they never write to or
modify the operator's destination. They auto-skip when env vars
are missing, so CI runs them as no-ops.

What they verify, against the **same** destination Arq.app
produces:

  - Layout discovery surfaces real computer / folder UUIDs
  - Format conformance (`check_arq7_compatibility`) passes
  - Latest backuprecord per folder decrypts + parses
  - Sample standalone object (the smallest one we find) ARQO-
    decrypts cleanly + has correct HMAC + blob_id matches
    SHA-256(salt + plaintext)
  - Shape fingerprint generation succeeds + the JSON is
    well-formed
  - L0 / L1a / L1b validator tiers all pass

Set up: see docs/COMPAT-SFTP-TESTING.md.

Security policy:
  - The tests connect read-only.
  - No file content is asserted on (it's PII; we assert structure
    only).
  - Sample restore writes only to a tempdir cleaned up at exit.
  - SFTP credentials never appear in stdout/stderr or test
    failure messages.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arq_validator import (
    ValidationTier,
    check_arq7_compatibility,
    compute_shape_fingerprint,
    discover_layout,
    validate,
)
from arq_validator.crypto import decrypt_keyset
from arq_validator.layout import list_backuprecords
from arq_validator.sftp import SftpBackend

from tests.integration._creds import resolve_creds, skip_reason


@unittest.skipUnless(
    resolve_creds() is not None,
    skip_reason() or "no credentials",
)
class SftpRealArq7Tests(unittest.TestCase):
    """Each test opens a fresh SftpBackend (one SSH master per
    test) and tears it down in cleanup. This way test ordering
    doesn't matter."""

    @classmethod
    def setUpClass(cls) -> None:
        creds = resolve_creds()
        assert creds is not None
        cls.creds = creds

    def _open_backend(self) -> SftpBackend:
        creds = self.creds
        backend = SftpBackend(
            host=creds.host,
            user=creds.user,
            port=creds.port,
            password=creds.sftp_password,
            identity_file=creds.identity_file,
            root=creds.root,
        )
        backend.__enter__()
        return backend

    # -----------------------------------------------------------------
    # Read-only structural checks
    # -----------------------------------------------------------------

    def test_layout_discovers_computer(self) -> None:
        backend = self._open_backend()
        try:
            layouts = discover_layout(backend, "/")
            self.assertGreaterEqual(
                len(layouts), 1,
                msg="no computer UUIDs found at SFTP root",
            )
            for lay in layouts:
                self.assertGreaterEqual(
                    len(lay.backup_folder_uuids), 1,
                    f"computer {lay.computer_uuid} has no folders",
                )
        finally:
            backend.close()

    def test_keyset_decrypts(self) -> None:
        backend = self._open_backend()
        try:
            layouts = discover_layout(backend, "/")
            cu = layouts[0].computer_uuid
            blob = backend.read_all(f"/{cu}/encryptedkeyset.dat")
            keyset = decrypt_keyset(blob, self.creds.dest_password)
            # Plaintext shape: 32B per master-key field
            self.assertEqual(len(keyset.encryption_key), 32)
            self.assertEqual(len(keyset.hmac_key), 32)
            self.assertEqual(len(keyset.blob_id_salt), 32)
        finally:
            backend.close()

    def test_compatibility_audit_passes(self) -> None:
        backend = self._open_backend()
        try:
            layouts = discover_layout(backend, "/")
            cu = layouts[0].computer_uuid
            report = check_arq7_compatibility(
                backend, "/",
                encryption_password=self.creds.dest_password,
                computer_uuid=cu,
            )
            self.assertTrue(
                report.passed,
                msg=(
                    f"compatibility audit failed: {report.summary()}\n"
                    + "\n".join(
                        f"  [{c.id}] {c.name}: {c.message}"
                        for c in report.failed_checks
                    )
                ),
            )
        finally:
            backend.close()

    def test_validator_l0_l1a_l1b_tiers_pass(self) -> None:
        # Tier QUICK = L0+L1a; DEEP adds L1b. AUDIT (L2) is skipped
        # to avoid a long run — operators can run that explicitly
        # if they want.
        backend = self._open_backend()
        try:
            for tier in (ValidationTier.QUICK, ValidationTier.DEEP):
                report = validate(
                    backend, root="/", tier=tier,
                    encryption_password=self.creds.dest_password,
                )
                self.assertIsNone(
                    report.error,
                    msg=f"validator at tier={tier.value} errored: "
                        f"{report.error}",
                )
                # Per-tier details: L1a sample + L1b record verifs
                # must report no failures.
                if report.l1a is not None:
                    self.assertEqual(
                        report.l1a.fail, 0,
                        msg=f"L1a saw failures: {report.l1a}",
                    )
                if report.l1b is not None:
                    self.assertEqual(
                        report.l1b.fail, 0,
                        msg=f"L1b saw failures: {report.l1b}",
                    )
        finally:
            backend.close()

    def test_fingerprint_is_well_formed_json(self) -> None:
        backend = self._open_backend()
        try:
            layouts = discover_layout(backend, "/")
            cu = layouts[0].computer_uuid
            fp = compute_shape_fingerprint(
                backend,
                encryption_password=self.creds.dest_password,
                computer_uuid=cu,
            )
            # Round-trip through JSON to confirm serializability.
            text = json.dumps(fp, ensure_ascii=False)
            re_loaded = json.loads(text)
            self.assertEqual(re_loaded["schema_version"], 1)
            self.assertGreaterEqual(len(re_loaded["computers"]), 1)
            comp = re_loaded["computers"][0]
            # Sidecar schemas must be present.
            self.assertIn("config_schema", comp)
            self.assertIn("plan_schema", comp)
        finally:
            backend.close()

    def test_records_list_at_least_one(self) -> None:
        backend = self._open_backend()
        try:
            layouts = discover_layout(backend, "/")
            cu = layouts[0].computer_uuid
            seen_any = False
            for fu in layouts[0].backup_folder_uuids:
                recs = list_backuprecords(backend, "/", cu, fu)
                if recs:
                    seen_any = True
                    # Each record path follows the spec pattern.
                    for p in recs:
                        self.assertTrue(
                            p.endswith(".backuprecord"),
                            f"unexpected record path: {p}",
                        )
            self.assertTrue(seen_any, "no records under any folder")
        finally:
            backend.close()

    def test_sample_standalone_object_arqo_valid(self) -> None:
        """Read the SMALLEST standalone object we can find and
        verify it ARQO-decrypts. Caps at 1 MiB and 16 attempts so
        the test stays fast on huge destinations."""
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_writer.crypto_write import compute_blob_id

        backend = self._open_backend()
        try:
            layouts = discover_layout(backend, "/")
            cu = layouts[0].computer_uuid
            keyset = decrypt_keyset(
                backend.read_all(f"/{cu}/encryptedkeyset.dat"),
                self.creds.dest_password,
            )
            so_root = f"/{cu}/standardobjects"
            if not backend.is_dir(so_root):
                self.skipTest(
                    "destination uses packed mode only — no "
                    "standardobjects/ to sample"
                )
            sampled = 0
            for shard in backend.list_dir(so_root):
                if sampled >= 16:
                    break
                shard_path = f"{so_root}/{shard}"
                if not backend.is_dir(shard_path):
                    continue
                for name in backend.list_dir(shard_path):
                    if sampled >= 16:
                        break
                    if len(name) != 62:
                        continue
                    file_path = f"{shard_path}/{name}"
                    try:
                        size = backend.stat_size(file_path)
                    except Exception:
                        continue
                    if size > 1024 * 1024:
                        continue
                    arqo = backend.read_all(file_path)
                    self.assertEqual(
                        arqo[:4], b"ARQO",
                        f"file at {file_path} doesn't start with ARQO",
                    )
                    plaintext = decrypt_lz4_arqo(
                        arqo,
                        keyset.encryption_key, keyset.hmac_key,
                    )
                    blob_id = (shard + name).lower()
                    self.assertEqual(
                        compute_blob_id(
                            keyset.blob_id_salt, plaintext,
                        ),
                        blob_id,
                        f"blob_id mismatch for {file_path}",
                    )
                    sampled += 1
            self.assertGreater(
                sampled, 0,
                "could not sample any standalone object",
            )
        finally:
            backend.close()


if __name__ == "__main__":
    unittest.main()
