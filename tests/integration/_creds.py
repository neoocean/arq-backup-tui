"""Credential resolution for the SFTP integration tests.

Three sources, evaluated in this order (first present wins for any
given field; missing fields fall through to the next source):

1. ``.secrets/`` — operator-managed JSON + plain-text files at the
   repo root. Preferred for long-lived workstation use because it
   keeps the credentials in one place that's easy to audit and
   easy to rotate. Layout::

       .secrets/
       ├── sftp.json          # {"host": "...", "port": 22, "user": "...",
       │                      #  "root": "/home/u123/arq",
       │                      #  "identity_file": "~/.ssh/id_ed25519"  OR
       │                      #  "password": "..." }
       └── dest_password      # one-line file: the Arq encryption
                              # password (NOT the SSH password)

2. ``.env`` at the repo root — single-file form parsed minimally
   (one ``KEY=VALUE`` per line, ``#`` comments + blank lines
   tolerated, one layer of matching quotes stripped).

3. ``os.environ`` — direct env-var override. Useful for CI
   secrets-store wiring and one-off ad-hoc runs.

Returns a :class:`Creds` dataclass or ``None`` when something
required is missing — the calling test then auto-skips with a
clear reason from :func:`skip_reason`.

Env vars (uppercase) used by ``.env`` / direct env mode:

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
  ARQ_TEST_WRITE_SUBDIR         (default
                                ``.arq-backup-tui-write-test``)
                                Subdirectory under
                                ``ARQ_TEST_SFTP_ROOT`` that the
                                writer integration test owns. Must
                                NOT match any UUID-shaped Arq
                                destination directory — it's
                                deliberately gitignored / dot-
                                prefixed so the operator's real
                                Arq.app-managed roots stay
                                untouched.

Either ``ARQ_TEST_SFTP_AUTH_PASSWORD`` (or ``password`` in
``sftp.json``) or ``ARQ_TEST_SFTP_IDENTITY`` (or ``identity_file``
in ``sftp.json``) must be set; both unset means the test skips
with "no SFTP auth material configured".

Never log the credentials. The helpers here only return a struct;
they don't print or persist anything.
"""

from __future__ import annotations

import json
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
    write_subdir: str

    @property
    def has_password_auth(self) -> bool:
        return bool(self.sftp_password)

    @property
    def write_subdir_path(self) -> str:
        """Absolute server-side path of the writer test's sandbox.

        Always under :attr:`root` and dot-prefixed by default so it
        sits alongside the operator's real destinations without
        looking like one (Arq destinations are UUID-named).
        """
        sub = self.write_subdir.lstrip("/")
        return f"{self.root.rstrip('/')}/{sub}"


# ---------------------------------------------------------------------------
# Source 1: .secrets/
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_from_secrets_dir() -> dict:
    """Return a partial creds dict pulled from
    ``<repo>/.secrets/``. Missing files / fields are simply absent
    from the returned dict; downstream merging fills the gaps from
    the other sources.

    Schema is intentionally tolerant: ``sftp.json`` may carry any
    subset of ``host`` / ``port`` / ``user`` / ``root`` /
    ``password`` / ``identity_file`` / ``write_subdir``. The
    encryption password lives in its own one-line file
    ``dest_password`` so the JSON file can be diffed without
    splatting secrets onto the screen.
    """
    out: dict = {}
    secrets_dir = _repo_root() / ".secrets"
    if not secrets_dir.is_dir():
        return out
    sftp_json = secrets_dir / "sftp.json"
    if sftp_json.is_file():
        try:
            data = json.loads(sftp_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            for key in (
                "host", "port", "user", "root",
                "password", "identity_file", "write_subdir",
            ):
                if key in data:
                    out[key] = data[key]
    pw_file = secrets_dir / "dest_password"
    if pw_file.is_file():
        try:
            text = pw_file.read_text(encoding="utf-8")
        except OSError:
            text = ""
        # Strip a single trailing newline (common when an editor
        # adds one) but preserve internal whitespace, since Arq
        # passwords can legitimately contain spaces.
        if text.endswith("\n"):
            text = text[:-1]
        if text:
            out["dest_password"] = text
    return out


# ---------------------------------------------------------------------------
# Source 2: .env (legacy)
# ---------------------------------------------------------------------------


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
        _repo_root() / ".env",
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
        return


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_creds() -> Optional[Creds]:
    """Return :class:`Creds` if every required field is present in
    at least one source (``.secrets/`` > ``.env`` > ``os.environ``),
    else None (caller skips with a reason)."""
    secrets = _load_from_secrets_dir()
    _load_dotenv_if_present()

    def pick(key_secret: str, env_var: str) -> Optional[str]:
        v = secrets.get(key_secret)
        if v is not None and v != "":
            return str(v)
        return os.environ.get(env_var) or None

    host = pick("host", "ARQ_TEST_SFTP_HOST")
    user = pick("user", "ARQ_TEST_SFTP_USER")
    root = pick("root", "ARQ_TEST_SFTP_ROOT")
    dest_password = pick("dest_password", "ARQ_TEST_DEST_PASSWORD")
    if not (host and user and root and dest_password):
        return None

    sftp_pw = pick("password", "ARQ_TEST_SFTP_AUTH_PASSWORD")
    identity = pick("identity_file", "ARQ_TEST_SFTP_IDENTITY") or ""
    identity_path = (
        Path(identity).expanduser() if identity else None
    )
    if not (sftp_pw or identity_path):
        return None

    port_raw = secrets.get("port") or os.environ.get(
        "ARQ_TEST_SFTP_PORT", "22",
    )
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 22

    write_subdir = (
        secrets.get("write_subdir")
        or os.environ.get("ARQ_TEST_WRITE_SUBDIR")
        or ".arq-backup-tui-write-test"
    )

    return Creds(
        host=host,
        port=port,
        user=user,
        root=root,
        sftp_password=sftp_pw,
        identity_file=identity_path,
        dest_password=dest_password,
        write_subdir=write_subdir,
    )


def skip_reason() -> Optional[str]:
    """Return a human-readable skip reason, or ``None`` if the
    env is fully configured."""
    if resolve_creds() is None:
        return (
            "real-SFTP integration tests skipped — no credentials "
            "in .secrets/, .env, or env vars (see "
            "docs/COMPAT-SFTP-TESTING.md)"
        )
    return None
