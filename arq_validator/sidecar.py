"""Sidecar JSON reader that handles both plain and ARQO-encrypted forms.

Arq.app v8 splits its top-level sidecar files into two categories:

- **Plain UTF-8 JSON**: ``backupconfig.json`` (top-level computer
  config) and ``backupfolders.json`` (storage-class index).
- **ARQO-encrypted UTF-8 JSON**: ``backupplan.json`` (top-level
  plan) and ``backupfolders/<UUID>/backupfolder.json`` (per-folder
  config). The encrypted forms wrap the plaintext JSON in the
  same ``ARQO`` envelope blob payloads use, but **without** the
  inner ``LZ4`` block compression — their plaintext is the raw
  pretty-printed JSON bytes.

This helper auto-detects the ``ARQO`` magic and decrypts when a
keyset is provided, so a single call site reads either form
correctly. Used by both the validator's compatibility audit
(``arq_validator/compatibility.py``) and its shape fingerprint
(``arq_validator/fingerprint.py``).

See ``docs/COMPAT-VERIFICATION.md`` §2.7.1 for the schema diff
that surfaced the encryption requirement.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .backend import Backend
from .crypto import Keyset


_ARQO_MAGIC = b"ARQO"


def read_sidecar(
    backend: Backend,
    path: str,
    keyset: Optional[Keyset] = None,
    *,
    openssl_path: str = "openssl",
) -> Optional[Dict[str, Any]]:
    """Read + parse a sidecar JSON file. Returns ``None`` on any
    failure (missing, unreadable, ARQO without keyset, decrypt
    failure, malformed JSON).

    When the file's first 4 bytes are ``b"ARQO"``, it must be
    decrypted with the destination's keyset to be parsed; without
    a keyset this function returns ``None`` rather than raise.
    Plain JSON files are parsed directly regardless of whether a
    keyset is supplied (``keyset`` is for opportunistic decrypt
    only — passing one is harmless on plain files).
    """
    try:
        raw = backend.read_all(path)
    except Exception:
        return None

    if raw[:4] == _ARQO_MAGIC:
        if keyset is None:
            return None
        # Local import keeps arq_validator.sidecar load-light when
        # only the plain-JSON path is exercised.
        from arq_reader.decrypt import decrypt_encrypted_object

        try:
            plain = decrypt_encrypted_object(
                raw,
                keyset.encryption_key,
                keyset.hmac_key,
                openssl_path=openssl_path,
            )
        except Exception:
            return None
    else:
        plain = raw

    try:
        return json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
