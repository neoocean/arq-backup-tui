"""Adapter from :class:`~arq_tui.state.Destination` to a live
``Backend`` instance.

The TUI never instantiates ``LocalBackend`` / ``SftpBackend``
inline; it always goes through :func:`open_backend`. Two reasons:

- Centralizes the path-vs-host coordinate handling so screens
  don't repeat it.
- Hides the SftpBackend context-manager dance so screens can
  treat the result as a plain backend object — the caller is
  responsible for invoking :func:`close_backend` when finished
  (typically via ``app.on_unmount``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from arq_validator.backend import Backend, LocalBackend
from arq_validator.sftp import SftpBackend

from .state import Destination


def open_backend(
    dest: Destination,
    *,
    sftp_password: Optional[str] = None,
) -> Backend:
    """Return an opened :class:`Backend` for ``dest``.

    For SFTP destinations the SSH master starts here (``__enter__``
    is invoked) so subsequent reads / writes share one
    authenticated session. ``sftp_password`` is consumed only for
    SFTP and only when no identity file was provided.

    Caller is responsible for invoking :func:`close_backend` when
    the backend is no longer needed.
    """
    if dest.kind == "local":
        path = Path(dest.path)
        # The writer expects to be able to create the destination
        # subtree; LocalBackend.__init__ refuses non-existent roots,
        # so materialize the directory here on the writer's behalf.
        path.mkdir(parents=True, exist_ok=True)
        return LocalBackend(path)
    if dest.kind == "sftp":
        backend = SftpBackend(
            host=dest.host,
            port=dest.port,
            user=dest.user,
            password=sftp_password,
            identity_file=Path(dest.identity_file) if dest.identity_file else None,
            root=dest.path,
        )
        backend.__enter__()
        return backend
    raise ValueError(f"unknown destination kind: {dest.kind!r}")


def close_backend(backend: Backend) -> None:
    """Tear down a backend opened via :func:`open_backend`.

    Idempotent for ``LocalBackend`` (no-op); calls ``__exit__`` /
    ``close`` on ``SftpBackend`` so the SSH master + ControlPath
    socket are released.
    """
    close = getattr(backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
