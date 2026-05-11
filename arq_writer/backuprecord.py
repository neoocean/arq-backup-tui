"""Build the ``backuprecord`` file.

A backup record is a property list (we emit binary plist) describing:

- A copy of the backup plan at backup time
- The root ``Node`` rendered as a nested dict (not the binary Node
  format, which lives inside ``treepacks/``)
- Some metadata (creation date, computer OS type, folder UUIDs, etc.)

The plist is then LZ4-wrapped and (when encryption is enabled, which
it always is for our writer) wrapped in an ``EncryptedObject``. The
output of :func:`build_backuprecord_arqo` is the raw bytes that go on
disk as ``backupfolders/<UUID>/backuprecords/<NNNNN>/<num>.backuprecord``.
"""

from __future__ import annotations

import json
import plistlib
import time
from typing import Any, Dict, List, Optional

from .crypto_write import build_encrypted_object
from .lz4_block import lz4_wrap
from .types import BlobLoc, FileNode, Node, TreeNode


def parse_backuprecord(plain: bytes) -> Dict[str, Any]:
    """Parse a decrypted backuprecord payload, accepting either
    Apple's binary plist (what our writer used to produce, before
    the JSON default) or UTF-8 JSON (what Arq.app actually emits).

    Discovered via real Hetzner Storage Box destination — see
    ``docs/REAL-DATA-DISCOVERIES.md`` §2. Both formats decode into
    a dict with the same shape, so callers don't need to know
    which one they got. Plist is tried first because legacy round-
    trip tests still cover it; on InvalidFileException we fall
    back to JSON.

    The same helper lives at ``arq_reader.restore._parse_backuprecord``
    for historical reasons; this is the canonical public copy.
    """
    try:
        record = plistlib.loads(plain)
        if isinstance(record, dict):
            return record
    except plistlib.InvalidFileException:
        pass
    try:
        record = json.loads(plain.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"backuprecord is neither binary plist nor UTF-8 JSON: "
            f"{exc}; first 32 bytes = {plain[:32]!r}"
        ) from exc
    if not isinstance(record, dict):
        raise ValueError(
            f"backuprecord JSON is not an object: "
            f"type={type(record).__name__}"
        )
    return record


def blobloc_to_dict(loc: BlobLoc) -> Dict[str, Any]:
    return {
        "blobIdentifier": loc.blobIdentifier,
        "isPacked": bool(loc.isPacked),
        "isLargePack": bool(loc.isLargePack),
        "relativePath": loc.relativePath,
        "offset": int(loc.offset),
        "length": int(loc.length),
        "stretchEncryptionKey": bool(loc.stretchEncryptionKey),
        "compressionType": int(loc.compressionType),
    }


def node_to_dict(node: Node) -> Dict[str, Any]:
    """Render a ``Node`` as the dict shape Arq.app v8 emits in the
    BackupRecord plist's ``node`` field.

    Sampled 2026-05-10 against ``/Volumes/arqbackup1`` (HANDOFF.md
    GAP-B). Per Arq.app v8 the JSON shape carries 9 keys our
    earlier emit was missing:

    - ``addedTime_sec`` / ``addedTime_nsec`` — when the entry was
      first added to a backup. We map this to ``create_time_*``
      because that's the closest semantic our writer tracks
      without an explicit per-entry add-time field.
    - ``documentID`` (int, default ``0``) /
      ``hasDocumentID`` (bool, default ``True``) — macOS document
      identifier. Constants for non-document files.
    - ``holes`` (list, default ``[]``) / ``isSparse`` (bool,
      default ``False``) / ``sparseLogicalSize`` (int, default
      ``0``) — sparse-file metadata. The writer doesn't probe for
      sparseness yet (separate enhancement); defaults match the
      empty / dense case Arq.app emits for ordinary files.
    - ``reparseTag`` (int) / ``reparsePointIsDirectory`` (bool) —
      Windows reparse-point fields. Same data the binary tree
      keeps in ``win_reparse_*``; renamed in JSON to match
      Arq.app's emit.

    Other key-name conventions (``changeTime_*`` /
    ``modificationTime_*`` / ``mac_st_*`` / ``winAttrs``) match the
    Arq 7 spec sample so the file is recognizable next to the spec.
    """
    is_tree = isinstance(node, TreeNode)
    out: Dict[str, Any] = {
        # When this entry first joined a backup. Best-effort proxy
        # is the file's create_time; Arq.app tracks this separately
        # but absent that infrastructure, the create-time is closer
        # than 0 (which would imply "never added").
        "addedTime_sec": int(node.create_time_sec),
        "addedTime_nsec": int(node.create_time_nsec),
        "isTree": bool(is_tree),
        "computerOSType": int(node.computerOSType),
        "containedFilesCount": int(node.containedFilesCount),
        "itemSize": int(node.itemSize),
        "modificationTime_sec": int(node.mtime_sec),
        "modificationTime_nsec": int(node.mtime_nsec),
        "changeTime_sec": int(node.ctime_sec),
        "changeTime_nsec": int(node.ctime_nsec),
        "creationTime_sec": int(node.create_time_sec),
        "creationTime_nsec": int(node.create_time_nsec),
        "deleted": bool(node.deleted),
        # macOS document ID. Arq.app v8 emits ``hasDocumentID:
        # True`` even when ``documentID`` is 0 (the operator's
        # real records consistently show this pair). Non-document
        # files end up with these defaults.
        "documentID": 0,
        "hasDocumentID": True,
        # ``userName`` / ``groupName`` round-trip the resolved
        # owner of the file as Arq.app records them. Discovered
        # against the operator's real Hetzner destination — our
        # earlier writer omitted both, which would block Arq.app
        # from showing UI ownership info on restore even though
        # the numeric ``mac_st_uid``/``gid`` was present.
        "userName": node.username or "",
        "groupName": node.groupName or "",
        "mac_st_dev": int(node.mac_st_dev),
        "mac_st_ino": int(node.mac_st_ino),
        "mac_st_mode": int(node.mac_st_mode),
        "mac_st_nlink": int(node.mac_st_nlink),
        "mac_st_uid": int(node.mac_st_uid),
        "mac_st_gid": int(node.mac_st_gid),
        "mac_st_rdev": int(node.mac_st_rdev),
        "mac_st_flags": int(node.mac_st_flags),
        # Sparse-file metadata. The writer doesn't probe sparseness
        # itself; emitting the defaults so Arq.app's loader sees a
        # well-formed shape. Real sparse-file detection is a
        # follow-up (HANDOFF.md F-series).
        "isSparse": False,
        "sparseLogicalSize": 0,
        "holes": [],
        # Windows reparse points. The Tree v4 binary keeps these
        # under ``win_reparse_*``; Arq.app's BackupRecord JSON
        # drops the ``win_`` prefix.
        "reparseTag": int(node.win_reparse_tag),
        "reparsePointIsDirectory": bool(
            node.win_reparse_point_is_directory
        ),
        "winAttrs": int(node.win_attrs),
    }
    if is_tree:
        assert isinstance(node, TreeNode)
        out["treeBlobLoc"] = blobloc_to_dict(node.treeBlobLoc)
        out["dataBlobLocs"] = []
    else:
        assert isinstance(node, FileNode)
        out["dataBlobLocs"] = [
            blobloc_to_dict(b) for b in node.dataBlobLocs
        ]
    out["xattrsBlobLocs"] = [
        blobloc_to_dict(b) for b in node.xattrsBlobLocs
    ]
    # ``aclBlobLoc`` — emit semantics refined by V4 (2026-05-11)
    # against the patched arq_restore. D2 added this field as
    # `null` for nodes without ACL; that broke arq_restore's
    # BSD reference parser because its ``Arq7BlobLoc initWithJSON:``
    # crashes when called on `NSNull` (the `objectForKey:` call
    # at Arq7BlobLoc.m sends to NSNull → unrecognized selector).
    #
    # A re-sample of real Arq.app v8 v4 records confirms Arq.app
    # OMITS the ``aclBlobLoc`` key entirely when no ACL is
    # present — not null, just absent. v3 records show the same
    # pattern (D2's "null for no-ACL" generalisation was based
    # on the parsed-dict view, where a missing key surfaces as
    # `None` via ``.get()`` — but that's a Python-side artefact,
    # not Arq.app's wire format).
    #
    # New emit rule: omit the key entirely when no ACL; emit a
    # BlobLoc dict otherwise. Our reader's ``.get('aclBlobLoc')``
    # tolerates both shapes (D2 reader-side fix at
    # arq_reader/restore.py:1138-1150 unaffected). The change
    # makes our v4 emit consumable by the patched arq_restore
    # binary (Strategy I-alt fresh-walk verification, §5.9).
    acl_loc = getattr(node, "aclBlobLoc", None)
    if acl_loc is not None:
        out["aclBlobLoc"] = blobloc_to_dict(acl_loc)
    return out


def build_backuprecord_dict(
    *,
    backup_folder_uuid: str,
    backup_plan_uuid: str,
    backup_plan_dict: Dict[str, Any],
    root_node: Node,
    local_path: str,
    local_mount_point: str = "/",
    relative_path: str = "",
    arq_version: str = "0.1.0",
    creation_date: Optional[float] = None,
    computer_os_type: int = 1,
    storage_class: str = "STANDARD",
    volume_name: str = "",
    version: Optional[int] = None,
    disk_identifier: str = "ROOT",
    backup_record_errors: Optional[List[Dict[str, Any]]] = None,
    node_tree_version: Optional[int] = None,
) -> Dict[str, Any]:
    """Assemble the top-level dict written into the backuprecord plist.

    ``node_tree_version`` (F2) is the binary tree-format version
    used for the root + every nested tree. When set, it lands as
    ``nodeTreeVersion`` and the record's ``version`` defaults to
    ``101`` (Arq.app v8's marker for Tree v4 records). When
    omitted, no ``nodeTreeVersion`` field is emitted and
    ``version`` defaults to ``100`` (the legacy Tree v3 record
    shape). Sampled 2026-05-10 against ``/Volumes/arqbackup1``:
    out of 352 real records, 333 use ``version=100`` with
    ``volumeName`` only and 18 use ``version=101`` with
    ``nodeTreeVersion=4`` (HANDOFF.md F2).

    ``backup_record_errors`` (default ``None`` = empty list)
    carries per-file errors collected during the walk. Each item
    is a dict matching Arq.app v8's per-error schema:

    - **required**: ``localPath: str``, ``errorMessage: str``,
      ``pathIsDirectory: bool``.
    - **optional** (set when the underlying error maps to an NSError):
      ``errorCode: int``, ``errorDomain: str``, ``severity: int``.

    Pre-T4 the writer emitted ``errorCount: 0`` (a scalar) here.
    The Arq.app v8 schema is a list-of-objects with the keys above
    — surfaced by the 2026-05-10 schema diff (T4 in HANDOFF.md).
    """
    if creation_date is None:
        creation_date = time.time()
    if version is None:
        version = 101 if node_tree_version is not None else 100
    rec: Dict[str, Any] = {
        "archived": False,
        "arqVersion": arq_version,
        "backupFolderUUID": backup_folder_uuid,
        "backupPlanJSON": backup_plan_dict,
        "backupPlanUUID": backup_plan_uuid,
        "backupRecordErrors": (
            list(backup_record_errors) if backup_record_errors else []
        ),
        "computerOSType": int(computer_os_type),
        "copiedFromCommit": False,
        "copiedFromSnapshot": False,
        "creationDate": int(creation_date),
        "diskIdentifier": disk_identifier,
        "isComplete": True,
        "localMountPoint": local_mount_point,
        "localPath": local_path,
        "node": node_to_dict(root_node),
        "relativePath": relative_path,
        "storageClass": storage_class,
        "version": int(version),
        "volumeName": volume_name,
    }
    if node_tree_version is not None:
        rec["nodeTreeVersion"] = int(node_tree_version)
    return rec


def serialize_backuprecord(
    record: Dict[str, Any], *, fmt: str = "json",
) -> bytes:
    """Serialize the backuprecord dict for on-disk storage.

    Two formats supported, switchable via ``fmt``:

    - ``"json"`` (default): UTF-8 JSON with no BOM. This is what
      **Arq.app actually emits** on real destinations — discovered
      against a Hetzner Storage Box where the operator's records
      started with ``{"backupFolderUUID":"…"}`` rather than the
      ``bplist00`` magic the spec describes. JSON is also the only
      format Arq.app's own restore code path appears to read on
      modern installs.
    - ``"binary-plist"``: legacy Apple binary plist via stdlib
      ``plistlib.dumps(fmt=FMT_BINARY)``. Kept for backward compat
      with destinations our writer produced before the JSON
      switch. The reader's ``_parse_backuprecord`` accepts both.

    JSON encoding follows Arq.app's conventions: dict keys preserved
    in their insertion order, ``ensure_ascii=False`` so non-ASCII
    paths round-trip transparently, no indent (matches the dense
    one-line format Arq.app emits).
    """
    if fmt == "binary-plist":
        return plistlib.dumps(record, fmt=plistlib.FMT_BINARY)
    if fmt != "json":
        raise ValueError(
            f"unknown backuprecord format: {fmt!r}; "
            f"expected 'json' or 'binary-plist'"
        )
    # Compact separators + Apple-style forward-slash escape match
    # Arq.app's actual emit byte-for-byte (Strategy F-2 verified
    # 2026-05-10 against /Volumes/arqbackup1):
    #
    # 1. Compact separators (``,`` and ``:`` with NO trailing space).
    #    Python's default ``json.dumps`` inserts a space after each
    #    delimiter, which is valid JSON but breaks per-blob byte
    #    equivalence with Arq.app's emit.
    # 2. ``/`` escaped as ``\/`` inside string values. Arq.app uses
    #    Apple's NSJSONSerialization, which emits the forward-slash
    #    escape (valid JSON, rarely seen elsewhere). Without the
    #    escape pass, paths like ``\/2DAC24D1.../treepacks/...``
    #    would land as ``/2DAC24D1.../treepacks/...`` and the byte
    #    diff would surface inside any ``relativePath`` string.
    #    The replacement is safe to do globally because ``/`` is
    #    only a literal character inside JSON string values (it has
    #    no meaning in keywords / structural tokens).
    text = json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("/", r"\/")
    return text.encode("utf-8")


def build_backuprecord_arqo(
    record_dict: Dict[str, Any],
    *,
    encryption_key: bytes,
    hmac_key: bytes,
    openssl_path: str = "openssl",
    session_key: Optional[bytes] = None,
    data_iv: Optional[bytes] = None,
    master_iv: Optional[bytes] = None,
) -> bytes:
    """Pipeline: plist -> LZ4 wrap -> ARQO. Returns on-disk bytes."""
    plist_bytes = serialize_backuprecord(record_dict)
    lz4_bytes = lz4_wrap(plist_bytes)
    return build_encrypted_object(
        lz4_bytes, encryption_key, hmac_key,
        openssl_path=openssl_path,
        session_key=session_key,
        data_iv=data_iv,
        master_iv=master_iv,
    )
