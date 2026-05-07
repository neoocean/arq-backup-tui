"""SFTP backend implementing the :class:`~arq_validator.backend.Backend`
protocol over OpenSSH's ssh / sftp client binaries.

Only the host's standard OpenSSH tools are required — no third-party
Python SSH library. The implementation uses a single ``ssh -N -M``
master process plus a ControlPath socket so all subsequent
``ssh`` / ``sftp`` invocations multiplex over one already-authenticated
TCP session. This pattern is robust against subtle launchd / sftp(1)
quirks that break ``sftp -fN`` and ``sftp`` standalone with password
authentication.

Three byte-range read paths cover the destinations we care about:

- ``head -c N <path>`` for prefix reads (offset == 0)
- ``dd if=<path> bs=1 skip=K count=N status=none`` for offset > 0
- ``sftp get`` for whole-file downloads to a temp path

The class is a context manager — the master is set up in ``__enter__``
and torn down in ``__exit__``. Construction does no I/O, so callers
can pass a partially-built ``SftpBackend`` around safely.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional


class SftpConnectionError(RuntimeError):
    """Raised when the SSH master cannot be established."""


class SftpBackend:
    """SSH/SFTP-backed implementation of the ``Backend`` protocol.

    Auth modes:

    - **password**: pass ``password=...``. The class writes a temporary
      ``SSH_ASKPASS`` shim with mode 0700 and points OpenSSH at it via
      ``SSH_ASKPASS_REQUIRE=force``. The password never appears in
      ``argv`` (so ``ps`` doesn't leak it).
    - **identity**: pass ``identity_file=Path(...)`` to use a
      specific private key. Default ssh identity files (``~/.ssh/id_*``)
      work without setting either field.

    Paths handed to backend methods are absolute SFTP paths (typically
    ``"/home/<computer-uuid>/...``). Unlike :class:`LocalBackend`,
    paths are not rewritten — the SFTP server is the root.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 22,
        user: str = "",
        password: Optional[str] = None,
        identity_file: Optional[os.PathLike] = None,
        connect_timeout_sec: int = 30,
        op_timeout_sec: int = 60,
        strict_host_key_checking: str = "accept-new",
        known_hosts_file: Optional[os.PathLike] = None,
        ssh_path: str = "ssh",
        sftp_path: str = "sftp",
    ) -> None:
        if not host:
            raise ValueError("host is required")
        self.host = host
        self.port = port
        self.user = user
        self._password = password
        self.identity_file = (
            Path(identity_file) if identity_file else None
        )
        self.connect_timeout_sec = connect_timeout_sec
        self.op_timeout_sec = op_timeout_sec
        self.strict_host_key_checking = strict_host_key_checking
        self.known_hosts_file = (
            Path(known_hosts_file) if known_hosts_file else None
        )
        self.ssh_path = ssh_path
        self.sftp_path = sftp_path

        self._askpass: Optional[Path] = None
        self._sock_dir: Optional[Path] = None
        self._sock: Optional[Path] = None
        self._master: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _user_at_host(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _common_ssh_opts(self) -> List[str]:
        opts = [
            "-o", f"StrictHostKeyChecking={self.strict_host_key_checking}",
            "-o", f"ConnectTimeout={self.connect_timeout_sec}",
        ]
        if self.known_hosts_file is not None:
            opts.extend(["-o", f"UserKnownHostsFile={self.known_hosts_file}"])
        if self.identity_file is not None:
            opts.extend([
                "-o", "IdentitiesOnly=yes",
                "-i", str(self.identity_file),
            ])
        if self._password is not None:
            # Force password auth via SSH_ASKPASS; refuse pubkey so the
            # askpass shim is actually consulted.
            opts.extend([
                "-o", "PubkeyAuthentication=no",
                "-o", "PreferredAuthentications=password",
                "-o", "BatchMode=no",
            ])
        return opts

    def _make_askpass(self, password: str) -> Path:
        fd, path = tempfile.mkstemp(prefix="arq-validator-askpass-", suffix=".sh")
        os.chmod(path, 0o700)
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/sh\n")
            f.write(f'printf "%s\\n" {shlex.quote(password)}\n')
        return Path(path)

    def __enter__(self) -> "SftpBackend":
        if self._master is not None:
            return self
        env = dict(os.environ)
        if self._password is not None:
            self._askpass = self._make_askpass(self._password)
            env["DISPLAY"] = "none:0"
            env["SSH_ASKPASS"] = str(self._askpass)
            env["SSH_ASKPASS_REQUIRE"] = "force"

        self._sock_dir = Path(
            tempfile.mkdtemp(prefix="arq-validator-ctrl-")
        )
        self._sock = self._sock_dir / "s"

        cmd = [
            self.ssh_path, "-N", "-M",
            "-S", str(self._sock),
            "-p", str(self.port),
            *self._common_ssh_opts(),
            self._user_at_host(),
        ]
        try:
            self._master = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            self._cleanup()
            raise SftpConnectionError(
                f"ssh not found at '{self.ssh_path}': {exc}"
            ) from exc

        deadline = time.monotonic() + self.connect_timeout_sec
        while time.monotonic() < deadline:
            if self._sock.exists():
                return self
            rc = self._master.poll()
            if rc is not None:
                err = (self._master.stderr.read() or b"").decode(
                    errors="replace"
                ).strip()[:300]
                self._cleanup()
                raise SftpConnectionError(
                    f"ssh master exited rc={rc} before ready: {err}"
                )
            time.sleep(0.2)
        self._cleanup()
        raise SftpConnectionError(
            f"ssh master did not become ready within "
            f"{self.connect_timeout_sec}s"
        )

    def __exit__(self, *exc) -> None:
        self._cleanup()

    def close(self) -> None:
        """Idempotent shutdown — useful when not using ``with``."""
        self._cleanup()

    def _cleanup(self) -> None:
        if self._sock and self._sock.exists():
            try:
                subprocess.run(
                    [
                        self.ssh_path, "-S", str(self._sock),
                        "-O", "exit", self._user_at_host(),
                    ],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
        if self._master is not None:
            try:
                if self._master.poll() is None:
                    self._master.terminate()
                self._master.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    self._master.kill()
                    self._master.wait(timeout=2)
                except Exception:
                    pass
            except Exception:
                pass
            self._master = None
        if self._askpass is not None:
            try:
                self._askpass.unlink()
            except OSError:
                pass
            self._askpass = None
        if self._sock_dir is not None:
            try:
                if self._sock and self._sock.exists():
                    self._sock.unlink()
                self._sock_dir.rmdir()
            except OSError:
                pass
            self._sock_dir = None
            self._sock = None

    # ------------------------------------------------------------------
    # Low-level command runners
    # ------------------------------------------------------------------

    def _require_open(self) -> None:
        if self._master is None or self._sock is None:
            raise SftpConnectionError(
                "SftpBackend is not open — use it as a context manager"
            )

    def _run_ssh(
        self, remote_cmd: str, *, timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        self._require_open()
        cmd = [
            self.ssh_path,
            "-S", str(self._sock),
            "-p", str(self.port),
            self._user_at_host(),
            remote_cmd,
        ]
        return subprocess.run(
            cmd, capture_output=True,
            timeout=timeout if timeout is not None else self.op_timeout_sec,
        )

    def _run_sftp_batch(
        self, batch_text: str, *, timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        self._require_open()
        fd, path = tempfile.mkstemp(
            prefix="arq-validator-batch-", suffix=".bat",
        )
        os.chmod(path, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(batch_text)
            cmd = [
                self.sftp_path,
                "-o", f"ControlPath={self._sock}",
                "-b", path,
                "-P", str(self.port),
                self._user_at_host(),
            ]
            return subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout if timeout is not None else self.op_timeout_sec,
            )
        finally:
            try:
                Path(path).unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Backend protocol
    # ------------------------------------------------------------------

    def list_dir(self, path: str) -> List[str]:
        cp = self._run_sftp_batch(f"ls -1 {shlex.quote(path)}\nbye\n")
        if cp.returncode != 0:
            raise RuntimeError(
                f"sftp ls -1 {path}: rc={cp.returncode} "
                f"err={(cp.stderr or '').strip()[:300]}"
            )
        names: List[str] = []
        for line in (cp.stdout or "").splitlines():
            s = line.strip()
            if not s or s in (".", "..") or s.startswith("sftp>"):
                continue
            if "/" in s:
                s = s.rsplit("/", 1)[-1]
            names.append(s)
        return sorted(set(names))

    def stat_size(self, path: str) -> int:
        cp = self._run_ssh(
            f"stat -c %s {shlex.quote(path)} 2>/dev/null || "
            f"stat -f %z {shlex.quote(path)}"
        )
        if cp.returncode != 0:
            raise RuntimeError(
                f"ssh stat {path}: rc={cp.returncode} "
                f"err={(cp.stderr.decode(errors='replace') or '').strip()[:300]}"
            )
        out = cp.stdout.decode(errors="replace").strip()
        try:
            return int(out)
        except ValueError as exc:
            raise RuntimeError(
                f"ssh stat {path}: unparseable output {out!r}"
            ) from exc

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError(
                f"read_range bad args: offset={offset} length={length}"
            )
        if length == 0:
            return b""
        if offset == 0:
            remote = f"head -c {length} {shlex.quote(path)}"
        else:
            remote = (
                f"dd if={shlex.quote(path)} bs=1 "
                f"skip={offset} count={length} status=none"
            )
        # Big reads (e.g. full backuprecord up to 50 MB) need a longer
        # timeout than the default per-op cap. Heuristic: 1 MB/s floor.
        timeout = max(
            self.op_timeout_sec,
            int(length / (1024 * 1024)) + self.op_timeout_sec,
        )
        cp = self._run_ssh(remote, timeout=timeout)
        if cp.returncode != 0:
            raise RuntimeError(
                f"ssh read_range {path} [{offset}+{length}]: "
                f"rc={cp.returncode} "
                f"err={(cp.stderr.decode(errors='replace') or '').strip()[:300]}"
            )
        return cp.stdout

    def read_all(self, path: str) -> bytes:
        return self.read_range(path, 0, self.stat_size(path))

    def exists(self, path: str) -> bool:
        cp = self._run_ssh(
            f"test -e {shlex.quote(path)} && echo Y || echo N",
            timeout=self.connect_timeout_sec,
        )
        return cp.returncode == 0 and cp.stdout.decode().strip() == "Y"

    def is_dir(self, path: str) -> bool:
        cp = self._run_ssh(
            f"test -d {shlex.quote(path)} && echo Y || echo N",
            timeout=self.connect_timeout_sec,
        )
        return cp.returncode == 0 and cp.stdout.decode().strip() == "Y"
