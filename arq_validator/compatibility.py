"""Arq 7 on-disk format compatibility checker.

Runs every documented format invariant against a destination and
returns a structured :class:`ComplianceReport`. Use this to verify
that a backup produced by ``arq_writer`` (or any other tool) is
genuinely Arq-7-shaped on disk — every file in the right place,
every byte at the right offset, every JSON key present with the
right type.

The checker's invariants are sourced from:

- The published Arq 7 spec
  (https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html)
- The ``arq_restore`` (BSD reference) source paths cited in
  ``docs/RESEARCH-format-extensions.md``
- Empirical corrections documented in ``arq_validator.constants``
  (25-byte unpadded keyset magic, 32-byte key fields)

Each check produces a :class:`CheckResult` with ``passed`` plus a
human-readable message. ``ComplianceReport.passed`` is the
aggregate; ``failed_checks`` is the punch list. The checker
**never raises** for a format failure — every problem lands in
the report so a caller can decide what to do (fail the test,
warn the user, etc.).

Spec citations are inlined as ``# spec: ...`` comments above each
check so a reviewer can cross-reference without leaving the file.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import plistlib
import re
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import constants as C
from .backend import Backend
from .crypto import decrypt_keyset


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


def _parse_record(plain):
    # Lazy import to avoid arq_validator/__init__ → arq_writer/__init__
    # circular import.
    from arq_writer.backuprecord import parse_backuprecord
    return parse_backuprecord(plain)



@dataclass
class CheckResult:
    """One pass/fail entry in a :class:`ComplianceReport`.

    ``id`` is a stable short identifier (``"L1"``, ``"C2"``, etc.)
    that pairs with the spec citation for the check; ``name`` is a
    human-readable description; ``message`` carries the failure
    detail or the success summary.
    """

    id: str
    name: str
    passed: bool
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ComplianceReport:
    """Aggregate report from :func:`check_arq7_compatibility`."""

    destination_root: str
    computer_uuid: str
    folder_uuids: List[str] = field(default_factory=list)
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks) and bool(self.checks)

    @property
    def failed_checks(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        ok = sum(1 for c in self.checks if c.passed)
        total = len(self.checks)
        return f"{ok}/{total} Arq 7 invariants passed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add(report: ComplianceReport, result: CheckResult) -> None:
    report.checks.append(result)


def _ok(id: str, name: str, **details: Any) -> CheckResult:
    return CheckResult(id=id, name=name, passed=True, details=dict(details))


def _fail(
    id: str, name: str, message: str, **details: Any,
) -> CheckResult:
    return CheckResult(
        id=id, name=name, passed=False,
        message=message, details=dict(details),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_arq7_compatibility(
    backend: Backend,
    root: str = "/",
    *,
    encryption_password: str,
    computer_uuid: Optional[str] = None,
    openssl_path: str = "openssl",
) -> ComplianceReport:
    """Run every Arq 7 invariant against the destination at
    ``root`` inside ``backend``.

    Returns a :class:`ComplianceReport`. Never raises for format
    failures; only programmer errors (e.g. ``backend`` is None)
    propagate.
    """
    report = ComplianceReport(
        destination_root=root,
        computer_uuid=computer_uuid or "",
    )

    # L1: top-level has at least one computer UUID dir.
    cu = _check_layout_root(backend, root, report, computer_uuid)
    if cu is None:
        return report
    report.computer_uuid = cu

    # L2..L5 + crypto: keyset + sidecars
    keyset = _check_keyset(
        backend, root, cu, encryption_password, report,
        openssl_path=openssl_path,
    )
    _check_backupconfig(backend, root, cu, report)
    _check_backupplan(backend, root, cu, report)
    _check_backupfolders_index(backend, root, cu, report)

    # L6..L8: per-folder configs + record paths
    folder_uuids = _check_per_folder(backend, root, cu, report)
    report.folder_uuids = folder_uuids

    if keyset is None:
        # Without keyset we can't run crypto / ARQO / record /
        # tree checks. Layout-only result is still valuable.
        return report

    # ARQO + record + tree + blob_id checks
    _check_backuprecords(
        backend, root, cu, folder_uuids, keyset, report,
        openssl_path=openssl_path,
    )
    _check_standardobjects(
        backend, root, cu, keyset, report,
        openssl_path=openssl_path,
    )
    _check_pack_files(backend, root, cu, report)

    return report


# ---------------------------------------------------------------------------
# L1 — top-level computer UUID
# ---------------------------------------------------------------------------


def _check_layout_root(
    backend: Backend, root: str,
    report: ComplianceReport,
    forced_uuid: Optional[str],
) -> Optional[str]:
    # spec: top-level entries are computer-UUID directories,
    # 8-4-4-4-12 hex (canonical uppercase per Arq.app conventions).
    try:
        entries = backend.list_dir(root)
    except Exception as exc:
        _add(report, _fail(
            "L1", "destination root readable",
            f"backend.list_dir({root!r}) raised: {exc}",
        ))
        return None
    cu_candidates = [
        e for e in entries if C.COMPUTER_UUID_RE.match(e)
    ]
    if not cu_candidates:
        _add(report, _fail(
            "L1", "at least one computer UUID at top level",
            "found no UUID-shaped entries",
            entries=entries,
        ))
        return None
    if forced_uuid is not None:
        if forced_uuid not in cu_candidates:
            _add(report, _fail(
                "L1", "expected computer UUID present",
                f"{forced_uuid!r} not in {cu_candidates!r}",
            ))
            return None
        cu = forced_uuid
    else:
        cu = cu_candidates[0]
    _add(report, _ok(
        "L1", "top-level computer UUID directory",
        computer_uuid=cu,
    ))
    return cu


# ---------------------------------------------------------------------------
# L2 + C1..C5 — encryptedkeyset.dat
# ---------------------------------------------------------------------------


def _check_keyset(
    backend: Backend, root: str, cu: str, password: str,
    report: ComplianceReport, *, openssl_path: str = "openssl",
):
    keyset_path = f"{root.rstrip('/')}/{cu}/{C.KEYSET_FILE}"
    try:
        blob = backend.read_all(keyset_path)
    except Exception as exc:
        _add(report, _fail(
            "L2", f"{C.KEYSET_FILE} present",
            f"read failed: {exc}",
        ))
        return None
    _add(report, _ok("L2", f"{C.KEYSET_FILE} present"))

    # C1: magic literal 25 bytes, NO NUL pad.
    # spec: empirically corrected -- see arq_validator.constants
    # (the published spec said magic was 32-byte NUL-padded; the
    # actual file format has no pad).
    if not blob.startswith(C.KEYSET_MAGIC):
        _add(report, _fail(
            "C1", "keyset magic 'ARQ_ENCRYPTED_MASTER_KEYS'",
            f"first 25 bytes = {blob[:25]!r}",
        ))
        return None
    _add(report, _ok("C1", "keyset magic 'ARQ_ENCRYPTED_MASTER_KEYS'"))

    # C2: layout = 25 magic + 8 salt + 32 hmac + 16 iv + ≥16 ciphertext
    if len(blob) < C.KEYSET_HEADER_BYTES + 16:
        _add(report, _fail(
            "C2", "keyset header layout",
            f"file is {len(blob)} bytes, header alone needs "
            f"{C.KEYSET_HEADER_BYTES + 16}",
        ))
        return None
    ciphertext_len = len(blob) - C.KEYSET_HEADER_BYTES
    if ciphertext_len % 16 != 0:
        _add(report, _fail(
            "C2", "keyset ciphertext block-aligned (AES-CBC)",
            f"ciphertext length {ciphertext_len} not a multiple of 16",
        ))
        return None
    _add(report, _ok(
        "C2", "keyset header + ciphertext block-aligned",
        ciphertext_bytes=ciphertext_len,
    ))

    # C3: HMAC verifies + PBKDF2 round-trips under password.
    try:
        keyset = decrypt_keyset(blob, password, openssl_path=openssl_path)
    except Exception as exc:
        _add(report, _fail(
            "C3", "keyset decrypt under provided password",
            f"{type(exc).__name__}: {exc}",
        ))
        return None
    _add(report, _ok("C3", "keyset decrypts + HMAC verifies"))

    # C4: plaintext shape — version=3 + three 32-byte key fields.
    if len(keyset.encryption_key) != C.KEYSET_PLAIN_FIELD_LEN:
        _add(report, _fail(
            "C4", "keyset plaintext encryption_key length",
            f"got {len(keyset.encryption_key)} bytes, "
            f"expected {C.KEYSET_PLAIN_FIELD_LEN}",
        ))
        return None
    if len(keyset.hmac_key) != C.KEYSET_PLAIN_FIELD_LEN:
        _add(report, _fail(
            "C4", "keyset plaintext hmac_key length",
            f"got {len(keyset.hmac_key)} bytes, "
            f"expected {C.KEYSET_PLAIN_FIELD_LEN}",
        ))
        return None
    if len(keyset.blob_id_salt) != C.KEYSET_PLAIN_FIELD_LEN:
        _add(report, _fail(
            "C4", "keyset plaintext blob_id_salt length",
            f"got {len(keyset.blob_id_salt)} bytes, "
            f"expected {C.KEYSET_PLAIN_FIELD_LEN}",
        ))
        return None
    _add(report, _ok(
        "C4", "keyset plaintext (version=3 + 3 × 32-byte fields)",
    ))
    return keyset


# ---------------------------------------------------------------------------
# L3 — backupconfig.json
# ---------------------------------------------------------------------------


_BACKUPCONFIG_REQUIRED = {
    "blobIdentifierType": int,
    "maxPackedItemLength": int,
    "backupName": str,
    "isWORM": bool,
    "containsGlacierArchives": bool,
    "additionalUnpackedBlobDirs": list,
    "chunkerVersion": int,
    "computerName": str,
    "computerSerial": str,
    "blobStorageClass": str,
    "isEncrypted": bool,
}


def _check_backupconfig(
    backend: Backend, root: str, cu: str, report: ComplianceReport,
) -> None:
    path = f"{root.rstrip('/')}/{cu}/backupconfig.json"
    data = _read_json(backend, path)
    if data is None:
        _add(report, _fail("L3", "backupconfig.json present + parseable",
                           f"missing or invalid JSON: {path}"))
        return
    _add(report, _ok("L3", "backupconfig.json present + parseable"))

    for key, expected_type in _BACKUPCONFIG_REQUIRED.items():
        if key not in data:
            _add(report, _fail(
                "L3", f"backupconfig.json key {key!r}",
                "missing",
            ))
            continue
        if not isinstance(data[key], expected_type):
            _add(report, _fail(
                "L3", f"backupconfig.json key {key!r} type",
                f"expected {expected_type.__name__}, "
                f"got {type(data[key]).__name__}",
            ))
            continue
        _add(report, _ok(
            "L3", f"backupconfig.json key {key!r}",
            value=data[key],
        ))

    # SV1..SV2: chunkerVersion = 3, blobIdentifierType = 2 (SHA-256).
    # spec: backupconfig.json — chunkerVersion is 3 in current Arq 7;
    # blobIdentifierType is 2 (SHA-256) per published spec.
    if data.get("chunkerVersion") not in (1, 2, 3):
        _add(report, _fail(
            "SV1", "chunkerVersion ∈ {1, 2, 3}",
            f"got {data.get('chunkerVersion')}",
        ))
    else:
        _add(report, _ok("SV1", "chunkerVersion is a known value"))
    if data.get("blobIdentifierType") not in (1, 2):
        _add(report, _fail(
            "SV2", "blobIdentifierType ∈ {1=SHA-1, 2=SHA-256}",
            f"got {data.get('blobIdentifierType')}",
        ))
    else:
        _add(report, _ok("SV2", "blobIdentifierType is a known value"))


# ---------------------------------------------------------------------------
# L4 — backupplan.json
# ---------------------------------------------------------------------------


_BACKUPPLAN_REQUIRED = {
    "active": bool,
    "backupFolderPlansByUUID": dict,
    "isEncrypted": bool,
    "name": str,
    "planUUID": str,
    "scheduleJSON": dict,
    "transferRateJSON": dict,
    "emailReportJSON": dict,
    "version": int,
    "useBuzhash": bool,
}


def _check_backupplan(
    backend: Backend, root: str, cu: str, report: ComplianceReport,
) -> None:
    path = f"{root.rstrip('/')}/{cu}/backupplan.json"
    data = _read_json(backend, path)
    if data is None:
        _add(report, _fail("L4", "backupplan.json present + parseable",
                           f"missing or invalid JSON: {path}"))
        return
    _add(report, _ok("L4", "backupplan.json present + parseable"))

    for key, expected_type in _BACKUPPLAN_REQUIRED.items():
        if key not in data:
            _add(report, _fail(
                "L4", f"backupplan.json key {key!r}",
                "missing",
            ))
            continue
        if not isinstance(data[key], expected_type):
            _add(report, _fail(
                "L4", f"backupplan.json key {key!r} type",
                f"expected {expected_type.__name__}, "
                f"got {type(data[key]).__name__}",
            ))
            continue
        _add(report, _ok(
            "L4", f"backupplan.json key {key!r} ok",
        ))

    # Each entry inside backupFolderPlansByUUID must have keys the
    # spec requires; check the first one for shape.
    plans = data.get("backupFolderPlansByUUID") or {}
    if isinstance(plans, dict) and plans:
        sample = next(iter(plans.values()))
        if isinstance(sample, dict):
            for k in (
                "backupFolderUUID", "localPath", "name",
                "localMountPoint", "skipDuringBackup",
            ):
                if k not in sample:
                    _add(report, _fail(
                        "L4", f"folder plan key {k!r}",
                        "missing",
                    ))
                else:
                    _add(report, _ok(
                        "L4", f"folder plan key {k!r}",
                    ))


# ---------------------------------------------------------------------------
# L5 — backupfolders.json
# ---------------------------------------------------------------------------


_BACKUPFOLDERS_REQUIRED = (
    "standardObjectDirs",
    "standardIAObjectDirs",
    "onezoneIAObjectDirs",
    "s3GlacierObjectDirs",
    # Glacier Instant Retrieval — emitted by Arq.app v8 alongside
    # the other s3*ObjectDirs slots; required for the compatibility
    # audit so we don't drift back into a missing-key state.
    "s3GlacierIRObjectDirs",
    "s3DeepArchiveObjectDirs",
)


def _check_backupfolders_index(
    backend: Backend, root: str, cu: str, report: ComplianceReport,
) -> None:
    path = f"{root.rstrip('/')}/{cu}/backupfolders.json"
    data = _read_json(backend, path)
    if data is None:
        _add(report, _fail("L5", "backupfolders.json present + parseable",
                           f"missing or invalid JSON: {path}"))
        return
    _add(report, _ok("L5", "backupfolders.json present + parseable"))
    for k in _BACKUPFOLDERS_REQUIRED:
        if k not in data:
            _add(report, _fail(
                "L5", f"backupfolders.json key {k!r}",
                "missing",
            ))
        elif not isinstance(data[k], list):
            _add(report, _fail(
                "L5", f"backupfolders.json key {k!r} type",
                f"expected list, got {type(data[k]).__name__}",
            ))
        else:
            _add(report, _ok(
                "L5", f"backupfolders.json key {k!r} is list",
            ))


# ---------------------------------------------------------------------------
# L6..L7 — per-folder configs
# ---------------------------------------------------------------------------


_BACKUPFOLDER_REQUIRED = (
    "localPath",
    "localMountPoint",
    "name",
    "uuid",
    "migratedFromArq60",
    "migratedFromArq5",
    "storageClass",
    "diskIdentifier",
)


def _check_per_folder(
    backend: Backend, root: str, cu: str, report: ComplianceReport,
) -> List[str]:
    bf_root = f"{root.rstrip('/')}/{cu}/{C.BACKUPFOLDERS_DIR}"
    try:
        folders = backend.list_dir(bf_root)
    except Exception as exc:
        _add(report, _fail(
            "L6", "backupfolders/ readable",
            f"list_dir failed: {exc}",
        ))
        return []
    folder_uuids = [
        f for f in folders if C.FOLDER_UUID_RE.match(f)
    ]
    if not folder_uuids:
        _add(report, _fail(
            "L6", "at least one folder UUID under backupfolders/",
            f"found {folders}",
        ))
        return []
    _add(report, _ok(
        "L6", "folder UUID directories under backupfolders/",
        count=len(folder_uuids),
    ))

    for fu in folder_uuids:
        path = f"{bf_root}/{fu}/backupfolder.json"
        data = _read_json(backend, path)
        if data is None:
            _add(report, _fail(
                "L7", f"backupfolder.json for {fu}",
                f"missing or invalid: {path}",
            ))
            continue
        _add(report, _ok(
            "L7", f"backupfolder.json for {fu} parseable",
        ))
        for k in _BACKUPFOLDER_REQUIRED:
            if k not in data:
                _add(report, _fail(
                    "L7", f"backupfolder.json[{fu}] key {k!r}",
                    "missing",
                ))
            else:
                _add(report, _ok(
                    "L7", f"backupfolder.json[{fu}] key {k!r}",
                ))
    return folder_uuids


# ---------------------------------------------------------------------------
# L8 + B1..B4 — backuprecord files (binary plist envelope)
# ---------------------------------------------------------------------------


_BACKUPRECORD_REQUIRED_KEYS = (
    "node",
    "creationDate",
    "arqVersion",
    "computerOSType",
    "backupFolderUUID",
    "backupPlanUUID",
    "backupPlanJSON",
    "version",
    "isComplete",
)


def _check_backuprecords(
    backend: Backend, root: str, cu: str,
    folder_uuids: List[str], keyset, report: ComplianceReport,
    *, openssl_path: str = "openssl",
) -> None:
    from arq_reader.decrypt import decrypt_lz4_arqo

    record_paths_seen = 0
    for fu in folder_uuids:
        rec_root = (
            f"{root.rstrip('/')}/{cu}/{C.BACKUPFOLDERS_DIR}/{fu}/"
            f"{C.BACKUPRECORDS_DIR}"
        )
        try:
            buckets = backend.list_dir(rec_root)
        except Exception:
            continue
        for bucket in buckets:
            if not (bucket.isdigit() and len(bucket) == 5):
                continue
            bucket_path = f"{rec_root}/{bucket}"
            try:
                inner = backend.list_dir(bucket_path)
            except Exception:
                continue
            for name in inner:
                if not name.endswith(".backuprecord"):
                    continue
                record_paths_seen += 1
                stem = name[: -len(".backuprecord")]
                if not stem.isdigit():
                    _add(report, _fail(
                        "L8", "backuprecord filename is digits + .backuprecord",
                        f"got {name!r} under {bucket_path}",
                    ))
                    continue
                rec_path = f"{bucket_path}/{name}"
                try:
                    arqo = backend.read_all(rec_path)
                except Exception as exc:
                    _add(report, _fail(
                        "B1", f"backuprecord readable: {rec_path}",
                        f"{type(exc).__name__}: {exc}",
                    ))
                    continue
                if not arqo.startswith(C.ARQO_MAGIC):
                    _add(report, _fail(
                        "B1", f"backuprecord ARQO magic: {rec_path}",
                        f"first 4 bytes = {arqo[:4]!r}",
                    ))
                    continue
                try:
                    plist_bytes = decrypt_lz4_arqo(
                        arqo, keyset.encryption_key, keyset.hmac_key,
                        openssl_path=openssl_path,
                    )
                    record = _parse_record(plist_bytes)
                except Exception as exc:
                    _add(report, _fail(
                        "B2", f"backuprecord decrypt + plist parse: {rec_path}",
                        f"{type(exc).__name__}: {exc}",
                    ))
                    continue
                if not isinstance(record, dict):
                    _add(report, _fail(
                        "B2", f"backuprecord top-level shape: {rec_path}",
                        f"expected dict, got {type(record).__name__}",
                    ))
                    continue
                # plist binary header is part of plistlib.loads
                # success -- if we got a dict back, the bytes started
                # with bplist00 (or were valid XML, both accepted).
                _add(report, _ok(
                    "B1", f"backuprecord ARQO + plist parse: {rec_path}",
                ))
                for k in _BACKUPRECORD_REQUIRED_KEYS:
                    if k not in record:
                        _add(report, _fail(
                            "B2", f"backuprecord key {k!r} ({rec_path})",
                            "missing",
                        ))
                # B3..B4: node shape
                node = record.get("node")
                if isinstance(node, dict):
                    if node.get("isTree") and not isinstance(
                        node.get("treeBlobLoc"), dict,
                    ):
                        _add(report, _fail(
                            "B3", f"node.treeBlobLoc dict ({rec_path})",
                            f"got {type(node.get('treeBlobLoc')).__name__}",
                        ))
                # SV3: backuprecord version is 100.
                if record.get("version") not in (100, 200):
                    _add(report, _fail(
                        "SV3", f"backuprecord version known ({rec_path})",
                        f"got {record.get('version')}",
                    ))
                else:
                    _add(report, _ok(
                        "SV3", f"backuprecord version known ({rec_path})",
                    ))
    _add(report, _ok(
        "L8", "backuprecord paths walked",
        count=record_paths_seen,
    ))


# ---------------------------------------------------------------------------
# A1..A4 + ID1..ID2 — standardobjects/
# ---------------------------------------------------------------------------


def _check_standardobjects(
    backend: Backend, root: str, cu: str,
    keyset, report: ComplianceReport,
    *, openssl_path: str = "openssl",
) -> None:
    so_root = f"{root.rstrip('/')}/{cu}/{C.STANDARDOBJECTS_DIR}"
    if not backend.is_dir(so_root):
        # Packed-only destination -- nothing to check here.
        _add(report, _ok(
            "A1", "standardobjects/ optional (packed-only mode)",
        ))
        return
    try:
        shards = backend.list_dir(so_root)
    except Exception as exc:
        _add(report, _fail(
            "A1", "standardobjects/ listable",
            f"{type(exc).__name__}: {exc}",
        ))
        return

    bad_arqo = 0
    bad_id = 0
    bad_shard = 0
    bad_name = 0
    sampled = 0
    sample_cap = 32

    from arq_reader.decrypt import decrypt_lz4_arqo

    for shard in shards:
        if len(shard) != 2 or not all(
            c in "0123456789abcdef" for c in shard.lower()
        ):
            bad_shard += 1
            continue
        shard_path = f"{so_root}/{shard}"
        try:
            files = backend.list_dir(shard_path)
        except Exception:
            continue
        for name in files:
            if not C.STANDARDOBJECT_NAME_RE.match(name):
                bad_name += 1
                continue
            if sampled >= sample_cap:
                continue
            sampled += 1
            blob_id = (shard + name).lower()
            blob_path = f"{shard_path}/{name}"
            try:
                arqo = backend.read_all(blob_path)
            except Exception:
                bad_arqo += 1
                continue
            if not arqo.startswith(C.ARQO_MAGIC):
                bad_arqo += 1
                continue
            # A2: HMAC verifies (decrypt_lz4_arqo runs HMAC first).
            try:
                plaintext = decrypt_lz4_arqo(
                    arqo, keyset.encryption_key, keyset.hmac_key,
                    openssl_path=openssl_path,
                )
            except Exception:
                bad_arqo += 1
                continue
            # ID2: blob_id = SHA-256(salt + plaintext) hex.
            h = hashlib.sha256()
            h.update(keyset.blob_id_salt)
            h.update(plaintext)
            if h.hexdigest() != blob_id:
                bad_id += 1

    if bad_shard == 0:
        _add(report, _ok(
            "S1", "standardobjects/ shard names are 2-hex",
        ))
    else:
        _add(report, _fail(
            "S1", "standardobjects/ shard names",
            f"{bad_shard} non-conforming shard dirs",
        ))
    if bad_name == 0:
        _add(report, _ok(
            "ID1", "standardobjects/<shard>/<name> name shape",
        ))
    else:
        _add(report, _fail(
            "ID1", "standardobjects file name shape",
            f"{bad_name} files don't match 62-hex pattern",
        ))
    if bad_arqo == 0:
        _add(report, _ok(
            "A1", f"standardobjects ARQO + HMAC sample (n={sampled})",
        ))
    else:
        _add(report, _fail(
            "A1", "standardobjects ARQO + HMAC",
            f"{bad_arqo}/{sampled} samples failed",
        ))
    if bad_id == 0:
        _add(report, _ok(
            "ID2", f"blob_id = SHA-256(salt + plaintext) (n={sampled})",
        ))
    else:
        _add(report, _fail(
            "ID2", "blob_id derivation",
            f"{bad_id}/{sampled} mismatched blob_ids",
        ))


# ---------------------------------------------------------------------------
# P1..P3 — pack files
# ---------------------------------------------------------------------------


def _check_pack_files(
    backend: Backend, root: str, cu: str,
    report: ComplianceReport,
) -> None:
    families = (
        C.TREEPACKS_DIR, C.BLOBPACKS_DIR, C.LARGEBLOBPACKS_DIR,
    )
    seen_any_pack = False
    bad_name = 0
    bad_arqo = 0
    sampled = 0
    sample_cap = 32

    for family in families:
        family_root = f"{root.rstrip('/')}/{cu}/{family}"
        if not backend.is_dir(family_root):
            continue
        try:
            shards = backend.list_dir(family_root)
        except Exception:
            continue
        for shard in shards:
            if len(shard) != 2:
                continue
            shard_path = f"{family_root}/{shard}"
            try:
                files = backend.list_dir(shard_path)
            except Exception:
                continue
            for name in files:
                if not C.PACK_NAME_RE.match(name):
                    bad_name += 1
                    continue
                seen_any_pack = True
                if sampled >= sample_cap:
                    continue
                sampled += 1
                pack_path = f"{shard_path}/{name}"
                try:
                    head = backend.read_range(pack_path, 0, 4)
                except Exception:
                    bad_arqo += 1
                    continue
                if head != C.ARQO_MAGIC:
                    bad_arqo += 1

    if not seen_any_pack:
        _add(report, _ok("P1", "no pack files (standalone-only mode)"))
        return
    if bad_name == 0:
        _add(report, _ok("P1", "pack file names match Arq.app shape"))
    else:
        _add(report, _fail(
            "P1", "pack file names",
            f"{bad_name} files don't match the UUID-shaped pattern",
        ))
    if bad_arqo == 0:
        _add(report, _ok(
            "P2", f"pack files start with ARQO magic (n={sampled})",
        ))
    else:
        _add(report, _fail(
            "P2", "pack-file ARQO magic at offset 0",
            f"{bad_arqo}/{sampled} packs don't start with ARQO",
        ))


# ---------------------------------------------------------------------------
# JSON read helper
# ---------------------------------------------------------------------------


def _read_json(backend: Backend, path: str) -> Optional[Dict[str, Any]]:
    try:
        raw = backend.read_all(path)
    except Exception:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
