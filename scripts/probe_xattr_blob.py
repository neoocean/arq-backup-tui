#!/usr/bin/env python3
"""Find one real ``xattrsBlobLocs`` entry on the operator's
destination, fetch + decrypt it, and print its bytes alongside
the parsed structure.

Why: PR #21 added xattr capture/restore using a single
binary-plist blob per Node (``{name: bytes_value, …}``). That's
our writer/reader contract. Arq.app may use a different scheme
— e.g. one blob per xattr, or a different per-attr framing.

This script scans the operator's destination for the first
FileNode that carries a non-empty ``xattrsBlobLocs`` list,
decrypts the first blob, and dumps:

1. Raw bytes (hex + length)
2. Auto-detect: binary plist? UTF-8 plist? raw bytes?
3. If it's a plist, the parsed dict shape

Output guides whether our writer needs to switch encoding to
match Arq.app.

Usage::

    python3 scripts/probe_xattr_blob.py [--max-walk 500] [--json]
"""

from __future__ import annotations

import argparse
import json
import plistlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _open_backend(creds):
    from arq_validator.sftp import SftpBackend
    backend = SftpBackend(
        creds.host, port=creds.port, user=creds.user,
        password=creds.sftp_password,
        identity_file=creds.identity_file,
        root=creds.root,
    )
    backend.__enter__()
    return backend


def _classify(blob: bytes) -> dict:
    """Inspect ``blob`` (already-decrypted) and report what it
    looks like."""
    out = {
        "length": len(blob),
        "full_hex": blob.hex(),
        "first_bytes_hex": blob[:32].hex(),
        "format_guess": "unknown",
        "parsed": None,
    }
    if not blob:
        out["format_guess"] = "empty"
        return out
    # Arq.app's own custom xattr format — discovered empirically
    # from the operator's destination. Magic = "XAttrSetV002".
    if blob.startswith(b"XAttrSetV002"):
        out["format_guess"] = "arq-XAttrSetV002"
        out["arq_xattr_decoded"] = _try_decode_arq_xattrset(blob)
        return out
    # Apple binary plist: starts with "bplist00".
    if blob.startswith(b"bplist00"):
        out["format_guess"] = "binary-plist"
        try:
            parsed = plistlib.loads(blob, fmt=plistlib.FMT_BINARY)
            out["parsed_shape"] = _summarize(parsed)
        except Exception as exc:
            out["parse_error"] = str(exc)
        return out
    # XML plist
    if blob[:6] == b"<?xml " or blob[:8] == b"<!DOCTYP":
        out["format_guess"] = "xml-plist"
        try:
            parsed = plistlib.loads(blob, fmt=plistlib.FMT_XML)
            out["parsed_shape"] = _summarize(parsed)
        except Exception as exc:
            out["parse_error"] = str(exc)
        return out
    # Single-xattr-value scheme: just raw bytes (no header).
    # We can't recover the name from this alone.
    out["format_guess"] = "raw-bytes"
    # Try printable preview as a best-effort hint.
    try:
        as_text = blob.decode("utf-8")
        if as_text.isprintable():
            out["text_preview"] = as_text[:200]
    except UnicodeDecodeError:
        pass
    return out


def _try_decode_arq_xattrset(blob: bytes) -> dict:
    """Best-effort decode of the ``XAttrSetV002`` format.

    Hypothesis (from initial 68-byte sample):
        bytes 0..11   = "XAttrSetV002"  (12 bytes magic)
        bytes 12..15  = ??? (4 bytes — possibly count or version)
        bytes 16..19  = ??? (4 bytes)
        bytes 20..23  = ??? (4 bytes)
        bytes 24..31  = uint64 BE = name_length
        bytes 32..32+name_length = xattr name
        bytes 32+name_length..??  = uint64 BE = value_length
        bytes ??..??+value_length = xattr value
    Repeats per-xattr. Refined by sampling more blobs.
    """
    import struct
    out = {
        "magic": blob[:12].decode("ascii", errors="replace"),
        "after_magic_hex": blob[12:32].hex(),
    }
    try:
        # Try interpreting bytes 24..31 as uint64 BE = first name length.
        name_len = struct.unpack(">Q", blob[24:32])[0]
        if 0 < name_len < 1024 and 32 + name_len <= len(blob):
            name = blob[32:32 + name_len].decode("utf-8", errors="replace")
            out["first_name"] = name
            out["first_name_length"] = name_len
            # Then maybe uint64 BE value length.
            vstart = 32 + name_len
            if vstart + 8 <= len(blob):
                value_len = struct.unpack(
                    ">Q", blob[vstart:vstart + 8],
                )[0]
                out["first_value_length"] = value_len
                if 0 <= value_len < len(blob):
                    value_bytes = blob[
                        vstart + 8:vstart + 8 + value_len
                    ]
                    out["first_value_hex"] = value_bytes.hex()
                    try:
                        out["first_value_text"] = (
                            value_bytes.decode("utf-8")
                            if value_bytes else ""
                        )
                    except UnicodeDecodeError:
                        pass
    except Exception as exc:
        out["decode_error"] = str(exc)
    return out


def _summarize(parsed) -> dict:
    """Compact representation of a parsed plist for logging."""
    if isinstance(parsed, dict):
        return {
            "type": "dict",
            "key_count": len(parsed),
            "keys": list(parsed.keys())[:10],
            "first_value_preview": (
                _value_preview(next(iter(parsed.values())))
                if parsed else None
            ),
        }
    if isinstance(parsed, list):
        return {
            "type": "list",
            "length": len(parsed),
            "first_preview": (
                _value_preview(parsed[0]) if parsed else None
            ),
        }
    return {"type": type(parsed).__name__,
            "preview": _value_preview(parsed)}


def _value_preview(v) -> dict:
    if isinstance(v, (bytes, bytearray)):
        return {
            "kind": "bytes",
            "length": len(v),
            "hex_preview": bytes(v[:16]).hex(),
        }
    if isinstance(v, str):
        return {
            "kind": "str",
            "length": len(v),
            "preview": v[:80],
        }
    return {"kind": type(v).__name__, "repr": repr(v)[:80]}


def _walk_for_first_xattr(
    backend, computer_uuid: str, password: str, *, max_walk: int,
):
    """Walk records → trees → nodes until we find the first node
    whose ``xattrsBlobLocs`` is non-empty. Returns
    ``(node_name, xattr_blob_loc)`` or ``None``."""
    from arq_reader.parse import parse_blobloc, BinaryReader
    from arq_validator.layout import (
        list_backuprecords, keyset_path,
    )
    from arq_validator.crypto import decrypt_keyset
    from arq_writer.backuprecord import parse_backuprecord
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree
    from arq_writer.types import FileNode, TreeNode

    keyset = decrypt_keyset(
        backend.read_all(keyset_path("/", computer_uuid)),
        password,
    )
    from arq_validator import discover_layout
    lay = next(
        lt for lt in discover_layout(
            backend, "/", enumerate_objects=False,
        ) if lt.computer_uuid == computer_uuid
    )
    walked = 0
    for fu in lay.backup_folder_uuids:
        records = list_backuprecords(
            backend, "/", computer_uuid, fu,
        )
        if not records:
            continue
        # Latest record only (older ones likely have the same
        # xattr layout).
        rec_blob = backend.read_all(records[-1])
        rec = parse_backuprecord(decrypt_lz4_arqo(
            rec_blob, keyset.encryption_key, keyset.hmac_key,
        ))
        node = rec.get("node") or {}
        # The root node's xattrsBlobLocs is our first candidate
        # — root dirs frequently carry com.apple.metadata:*.
        root_xattrs = node.get("xattrsBlobLocs") or []
        if root_xattrs:
            return ("root", root_xattrs[0])
        # Otherwise descend into root tree blob and look at children.
        tree_loc = node.get("treeBlobLoc") or {}
        if not tree_loc.get("blobIdentifier"):
            continue
        # Fetch root tree.
        from arq_reader.restore import Restore
        rs = Restore(
            "/", encryption_password=password, backend=backend,
        )
        # Walk the tree manually, look for any FileNode w/ xattrs.
        stack = [(tree_loc, "")]
        while stack and walked < max_walk:
            loc, parent_rel = stack.pop()
            walked += 1
            try:
                if loc.get("isPacked"):
                    raw = backend.read_range(
                        loc["relativePath"],
                        int(loc["offset"]),
                        int(loc["length"]),
                    )
                else:
                    raw = backend.read_all(loc["relativePath"])
                tree_plain = decrypt_lz4_arqo(
                    raw, keyset.encryption_key, keyset.hmac_key,
                )
                tree = parse_tree(tree_plain)
            except Exception as exc:
                continue
            for child in tree.children:
                child_rel = (
                    f"{parent_rel}/{child.name}"
                    if parent_rel else child.name
                )
                node = child.node
                xattrs = getattr(node, "xattrsBlobLocs", None) or []
                if xattrs:
                    print(
                        f"found xattr-bearing node: {child_rel} "
                        f"({len(xattrs)} xattr blob(s))",
                        file=sys.stderr,
                    )
                    return (child_rel, xattrs[0])
                if isinstance(node, TreeNode):
                    # Convert to dict shape for the iteration.
                    sub_loc = {
                        "blobIdentifier":
                            getattr(node.treeBlobLoc, "blobIdentifier", ""),
                        "relativePath":
                            getattr(node.treeBlobLoc, "relativePath", ""),
                        "offset":
                            int(getattr(node.treeBlobLoc, "offset", 0)),
                        "length":
                            int(getattr(node.treeBlobLoc, "length", 0)),
                        "isPacked":
                            bool(getattr(node.treeBlobLoc, "isPacked", False)),
                    }
                    stack.append((sub_loc, child_rel))
            if walked >= max_walk:
                break
    return None


def _main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-walk", type=int, default=500)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        from tests.integration._creds import (
            resolve_creds, skip_reason,
        )
    except ImportError as exc:
        sys.exit(f"can't import creds helper: {exc}")
    creds = resolve_creds()
    if creds is None:
        sys.exit(
            f"creds unavailable: {skip_reason() or 'no creds'}"
        )

    backend = _open_backend(creds)
    try:
        from arq_validator import discover_layout
        layouts = list(discover_layout(
            backend, "/", enumerate_objects=False,
        ))
        if not layouts:
            sys.exit("no computer subtrees found")
        cu = layouts[0].computer_uuid
        result = _walk_for_first_xattr(
            backend, cu, creds.dest_password,
            max_walk=args.max_walk,
        )
        if result is None:
            print(
                "no xattr-bearing node found in walk; "
                "operator's destination may not carry xattrs",
                file=sys.stderr,
            )
            return 1
        node_name, loc = result
        # Fetch + decrypt the xattr blob.
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.layout import keyset_path
        from arq_reader.decrypt import decrypt_lz4_arqo
        keyset = decrypt_keyset(
            backend.read_all(keyset_path("/", cu)),
            creds.dest_password,
        )
        if loc.isPacked if hasattr(loc, "isPacked") else loc.get("isPacked"):
            rel = loc.relativePath if hasattr(loc, "relativePath") else loc["relativePath"]
            off = int(loc.offset if hasattr(loc, "offset") else loc["offset"])
            ln = int(loc.length if hasattr(loc, "length") else loc["length"])
            arqo = backend.read_range(rel, off, ln)
        else:
            rel = loc.relativePath if hasattr(loc, "relativePath") else loc["relativePath"]
            arqo = backend.read_all(rel)
        plain = decrypt_lz4_arqo(
            arqo, keyset.encryption_key, keyset.hmac_key,
        )
        info = _classify(plain)
        info["node"] = node_name
        info["blob_loc_relative_path"] = (
            loc.relativePath if hasattr(loc, "relativePath")
            else loc.get("relativePath", "")
        )
        if args.json:
            print(json.dumps(info, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"node:               {info['node']}")
            print(f"blob path:          {info['blob_loc_relative_path']}")
            print(f"plaintext length:   {info['length']}")
            print(f"first 32 bytes:     {info['first_bytes_hex']}")
            print(f"format guess:       {info['format_guess']}")
            if "parsed_shape" in info:
                print(f"parsed:             {info['parsed_shape']}")
            elif "text_preview" in info:
                print(f"text preview:       {info['text_preview']!r}")
            elif "parse_error" in info:
                print(f"parse error:        {info['parse_error']}")
        return 0
    finally:
        try:
            backend.__exit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(_main())
