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
from typing import Any, Dict, Optional

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
    """Render a ``Node`` as the dict shape the spec shows in the example.

    Field names match the ASCII-plist sample in the Arq 7 spec
    (``changeTime_*``, ``modificationTime_*``, ``mac_st_*``, etc.) so a
    reader who has the spec open can recognize them at a glance.
    """
    is_tree = isinstance(node, TreeNode)
    out: Dict[str, Any] = {
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
    version: int = 100,
    disk_identifier: str = "ROOT",
) -> Dict[str, Any]:
    """Assemble the top-level dict written into the backuprecord plist."""
    if creation_date is None:
        creation_date = time.time()
    return {
        "archived": False,
        "arqVersion": arq_version,
        "backupFolderUUID": backup_folder_uuid,
        "backupPlanJSON": backup_plan_dict,
        "backupPlanUUID": backup_plan_uuid,
        "computerOSType": int(computer_os_type),
        "copiedFromCommit": False,
        "copiedFromSnapshot": False,
        "creationDate": int(creation_date),
        "diskIdentifier": disk_identifier,
        "errorCount": 0,
        "isComplete": True,
        "localMountPoint": local_mount_point,
        "localPath": local_path,
        "node": node_to_dict(root_node),
        "relativePath": relative_path,
        "storageClass": storage_class,
        "version": int(version),
        "volumeName": volume_name,
    }


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
    return json.dumps(record, ensure_ascii=False).encode("utf-8")


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
