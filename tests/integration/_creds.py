"""Credential resolution for the SFTP integration tests.

Reads from environment variables (preferred) or, optionally, a
local ``.env`` file at the repo root. Returns a ``Creds`` dataclass
or ``None`` when something required is missing — the calling test
then auto-skips with a clear reason.

Env vars (all uppercase, all optional unless marked required):

  ARQ_TEST_SFTP_HOST            (required)  hostname or IP
  ARQ_TEST_SFTP_USER            (required)  SSH username
  ARQ_TEST_SFTP_PORT            (default 22)
  ARQ_TEST_SFTP_ROOT            (required)  server-side path to the
                                            destination root
                                            (e.g. /home/u123/arq)
  ARQ_TEST_SFTP_AUTH_PASSWORD               SSH password (if used)
  ARQ_TEST_SFTP_IDENTITY                    Path to private key
  ARQ_TEST_DEST_PASSWORD        (required)  Encryption password
                                            for the Arq destination
                                            (NOT the SSH password)

Either ``ARQ_TEST_SFTP_AUTH_PASSWORD`` or ``ARQ_TEST_SFTP_IDENTITY``
must be set; both unset means the test skips with "no SFTP auth
material configured".

Never log the credentials. The helpers here only return a struct;
they don't print or persist anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Creds:
    host: str
    port: int
    user: str
    root: str
    sftp_password: Optional[str]
    identity_file: Optional[Path]
    dest_password: str

    @property
    def has_password_auth(self) -> bool:
        return bool(self.sftp_password)


def _load_dotenv_if_present() -> None:
    """If a ``.env`` file exists at the repo root, parse it and set
    any vars that aren't already in ``os.environ``.

    Format is one ``KEY=VALUE`` per line; blank lines and ``#``
    comments are ignored. Quoted values have one layer of quotes
    stripped. The parser is intentionally minimal; complex shell
    escapes aren't supported.
    """
    candidates = [
        Path(__file__).resolve().parent / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if v and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            os.environ.setdefault(k, v)
        # First file wins.
        return


def resolve_creds() -> Optional[Creds]:
    """Return :class:`Creds` if every required env var is set, else
    None (caller skips with a reason)."""
    _load_dotenv_if_present()
    host = os.environ.get("ARQ_TEST_SFTP_HOST")
    user = os.environ.get("ARQ_TEST_SFTP_USER")
    root = os.environ.get("ARQ_TEST_SFTP_ROOT")
    dest_password = os.environ.get("ARQ_TEST_DEST_PASSWORD")
    if not (host and user and root and dest_password):
        return None
    sftp_pw = os.environ.get("ARQ_TEST_SFTP_AUTH_PASSWORD") or None
    identity = os.environ.get("ARQ_TEST_SFTP_IDENTITY") or ""
    identity_path = (
        Path(identity).expanduser() if identity else None
    )
    if not (sftp_pw or identity_path):
        return None
    try:
        port = int(os.environ.get("ARQ_TEST_SFTP_PORT", "22"))
    except ValueError:
        port = 22
    return Creds(
        host=host,
        port=port,
        user=user,
        root=root,
        sftp_password=sftp_pw,
        identity_file=identity_path,
        dest_password=dest_password,
    )


def skip_reason() -> Optional[str]:
    """Return a human-readable skip reason, or ``None`` if the
    env is fully configured."""
    if resolve_creds() is None:
        return (
            "real-SFTP integration tests skipped — no credentials "
            "in env (see docs/COMPAT-SFTP-TESTING.md)"
        )
    return None
