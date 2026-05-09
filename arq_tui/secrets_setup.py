"""Write/refresh ``.secrets/sftp.json`` + ``.secrets/dest_password``
from values the operator just typed into the destination wizard.

The ``.secrets/`` directory is what the integration tests +
cron / health-check workflows read from. When the operator
configures an SFTP destination through the TUI they typically
ALSO want those credentials available to the scheduler later;
making them re-type host / user / path / password into a JSON
file would be a real friction point.

This module is the small bridge:

- :func:`write_secrets_for_destination(dest, password,
  dest_password)` — writes the JSON with mode 0600 + (optionally)
  the encryption-password file with mode 0600
- :func:`secrets_dir_writable()` — operator-facing precheck so
  the caller can show "secrets dir is read-only, install
  manually" without crashing

Read-only operations (the integration test fixture loader) live
in :mod:`tests.integration._creds`; this module only writes.
Both modules use the same JSON schema documented in
``docs/COMPAT-SFTP-TESTING.md``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def secrets_dir(repo_root: Optional[Path] = None) -> Path:
    """Return the ``.secrets/`` directory path under the repo
    root. Default ``repo_root`` walks up from this module's
    parent until we hit the repo (the directory containing
    ``pyproject.toml``)."""
    if repo_root is not None:
        return repo_root / ".secrets"
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor / ".secrets"
    # Fallback: user's home, hidden subdir.
    return Path.home() / ".arq-backup-tui-secrets"


def secrets_dir_writable(
    repo_root: Optional[Path] = None,
) -> bool:
    """Check whether we can create / write into ``.secrets/``.

    Used by the wizard's pre-write precheck — when the dir is
    read-only (e.g. on a locked installation) the wizard shows
    a hint instead of trying to write + failing."""
    d = secrets_dir(repo_root)
    parent = d if d.exists() else d.parent
    return os.access(parent, os.W_OK)


def write_sftp_json(
    *,
    host: str,
    user: str,
    port: int = 22,
    root: str,
    identity_file: Optional[str] = None,
    sftp_password: Optional[str] = None,
    write_subdir: str = ".arq-backup-tui-write-test",
    repo_root: Optional[Path] = None,
) -> Path:
    """Write ``.secrets/sftp.json`` with the supplied fields.

    Returns the absolute path written. Mode is 0600 so other
    users on the host can't read it. Existing file is overwritten
    atomically (write to a sibling ``.tmp`` file + ``os.replace``).
    The shape matches what :mod:`tests.integration._creds`'s
    ``_load_from_secrets_dir`` reads.
    """
    d = secrets_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    out = d / "sftp.json"
    payload = {
        "host": host,
        "port": int(port),
        "user": user,
        "root": root,
        "write_subdir": write_subdir,
    }
    if identity_file:
        payload["identity_file"] = identity_file
    if sftp_password:
        payload["password"] = sftp_password
    tmp = out.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, out)
    return out


def write_dest_password(
    dest_password: str,
    *,
    repo_root: Optional[Path] = None,
) -> Path:
    """Write ``.secrets/dest_password`` (the Arq encryption
    password — not the SSH password) with mode 0600. Atomic
    overwrite of any existing file."""
    d = secrets_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    out = d / "dest_password"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(dest_password, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, out)
    return out


def write_secrets_for_destination(
    *,
    host: str, user: str, port: int, root: str,
    identity_file: Optional[str] = None,
    sftp_password: Optional[str] = None,
    dest_password: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> dict:
    """One-shot wrapper used by the TUI wizard. Writes whichever
    files have content. Returns ``{"sftp_json": path|None,
    "dest_password": path|None}`` so the caller can show
    "wrote 2 files in .secrets/"."""
    out = {"sftp_json": None, "dest_password": None}
    if host and user and root:
        out["sftp_json"] = write_sftp_json(
            host=host, user=user, port=port, root=root,
            identity_file=identity_file,
            sftp_password=sftp_password,
            repo_root=repo_root,
        )
    if dest_password:
        out["dest_password"] = write_dest_password(
            dest_password, repo_root=repo_root,
        )
    return out
