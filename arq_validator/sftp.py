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
from typing import Callable, Dict, List, Optional


class SftpConnectionError(RuntimeError):
    """Raised when the SSH master cannot be established."""


class SftpRateLimitedError(SftpConnectionError):
    """Raised when the backend is hitting a connection-rate limit
    on the server side (Hetzner Storage Box being the common one).

    Triggered after :class:`_RateLimitTracker.threshold` consecutive
    SSH / SFTP commands fail with a ``Connection refused`` /
    ``mux_client_request_session`` style error, meaning the upstream
    is throttling new sessions on the multiplexed master and
    further attempts within the rate-limit window will keep
    failing. Callers should back off (sleep tens of seconds) before
    retrying, or surface to the user that the destination is
    refusing connections.
    """


# Stderr substrings that indicate the server is refusing new
# sessions on the multiplexed master rather than a real protocol
# fault. Hetzner Storage Box hits the first three within seconds
# of opening more than ~10 sftp-bursting subprocesses; the fourth
# is what OpenSSH prints when the master itself dropped the new
# session under load.
_RATE_LIMIT_PATTERNS = (
    "Connection refused",
    "Connection reset by peer",
    "mux_client_request_session",
    "no master connection",
    "Control socket connect",
    "channel_setup_fwd_listener",
    "Connection closed by remote host",
)


class _RateLimitTracker:
    """Counts consecutive Hetzner-style rate-limit failures.

    A single ``SftpBackend`` owns one tracker. Each command run
    through the backend funnels its ``stderr`` here via
    :meth:`record`; matches against any of
    :data:`_RATE_LIMIT_PATTERNS` increment a streak counter, and
    once it reaches ``threshold`` the backend raises
    :class:`SftpRateLimitedError` so callers fail fast instead of
    flooding the upstream with doomed retries.

    ``threshold = 20`` mirrors the reference operator-side
    `arq-validate.py` value tuned against Hetzner Storage Box.
    """

    def __init__(self, threshold: int = 20) -> None:
        self.threshold = threshold
        self.consecutive_failures = 0
        # Total over the lifetime of the backend (for diagnostics).
        self.total_failures = 0
        self.last_pattern: Optional[str] = None

    def record(
        self, returncode: int, stderr_text: str,
    ) -> Optional[str]:
        """Inspect one command result. Returns the matched pattern
        on hit, ``None`` otherwise. Callers raise
        :class:`SftpRateLimitedError` when ``consecutive_failures``
        crosses the threshold; this method does not raise itself
        so the wrapping context can decide when to surface.
        """
        if returncode == 0:
            self.consecutive_failures = 0
            self.last_pattern = None
            return None
        for pat in _RATE_LIMIT_PATTERNS:
            if pat in stderr_text:
                self.consecutive_failures += 1
                self.total_failures += 1
                self.last_pattern = pat
                return pat
        # Non-rate-limit failure resets the streak — the next
        # command is what counts.
        self.consecutive_failures = 0
        self.last_pattern = None
        return None

    def threshold_hit(self) -> bool:
        return self.consecutive_failures >= self.threshold


# ---------------------------------------------------------------------------
# Adaptive backoff
# ---------------------------------------------------------------------------


class _BackoffPolicy:
    """Exponential-with-jitter backoff for transient SFTP errors.

    Only the *first* command after a rate-limit hit sleeps the
    base interval; subsequent consecutive hits double the wait,
    capped at :attr:`max_sleep_sec`. A clean run resets the
    counter to 0 so the next isolated hit doesn't immediately
    sleep for minutes.

    The policy stays small + state-only: callers ask
    :meth:`sleep_for` to compute the next sleep, then sleep on
    their own (usually inside a try/except retry loop). This
    separation keeps the policy unit-testable without sleeping in
    the test process.
    """

    def __init__(
        self,
        *,
        base_sec: float = 2.0,
        factor: float = 2.0,
        max_sleep_sec: float = 300.0,
        max_attempts: int = 5,
        jitter_fraction: float = 0.25,
    ) -> None:
        self.base_sec = base_sec
        self.factor = factor
        self.max_sleep_sec = max_sleep_sec
        self.max_attempts = max_attempts
        self.jitter_fraction = jitter_fraction

    def sleep_for(self, attempt: int) -> float:
        """Return the backoff (seconds) to wait *before* attempt N
        (0-indexed). Attempt 0 = no sleep yet (first try); attempt
        1 = base; attempt 2 = base*factor; etc., capped at
        :attr:`max_sleep_sec` and jittered by ``±jitter_fraction``.
        """
        import random
        if attempt <= 0:
            return 0.0
        raw = self.base_sec * (self.factor ** (attempt - 1))
        capped = min(raw, self.max_sleep_sec)
        jitter = capped * self.jitter_fraction
        return max(0.0, capped + random.uniform(-jitter, jitter))


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
        root: str = "",
        rate_limit_abort_threshold: int = 20,
        backoff_base_sec: float = 2.0,
        backoff_max_sec: float = 300.0,
        backoff_max_attempts: int = 5,
        backoff_callback: Optional[
            "Callable[[str, Dict], None]"
        ] = None,
    ) -> None:
        """``root`` (optional): when set, every backend-method path
        is interpreted relative to this server-side prefix. Use it
        when a caller wants to address the backup via root-relative
        paths like ``"/<cu>/standardobjects/..."`` rather than
        absolute server paths. Validator callers that already build
        absolute paths leave ``root=""`` (the default) unchanged.
        """
        if not host:
            raise ValueError("host is required")
        self.host = host
        self.port = port
        self.user = user
        self._password = password
        self.root = root.rstrip("/")
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
        # Hetzner-style upstream rate-limit detector. Inspected
        # after every _run_ssh / _run_sftp_batch call; raises
        # SftpRateLimitedError once consecutive_failures hits
        # ``rate_limit_abort_threshold`` so the caller can back
        # off rather than flood the server with doomed retries.
        self._rate_limit = _RateLimitTracker(
            threshold=rate_limit_abort_threshold,
        )
        # Adaptive backoff that wraps every _run_ssh / _run_sftp_batch
        # call. When the underlying command's stderr looks like a
        # rate-limit pattern we sleep + retry up to backoff_max_attempts
        # before surfacing the error. Decoupled from the streak counter
        # so a successful retry resets streak (matching the existing
        # tracker semantics) but still consumed an attempt budget.
        self._backoff = _BackoffPolicy(
            base_sec=backoff_base_sec,
            max_sleep_sec=backoff_max_sec,
            max_attempts=backoff_max_attempts,
        )
        self._backoff_callback = backoff_callback
        # Per-session whole-file cache for the SFTP-only fallback in
        # ``read_range``. Chrooted endpoints (Hetzner Storage Box)
        # reject ssh shell commands, so byte-range reads have to
        # download the whole file via ``sftp get``. Without this
        # cache, a single restore of an Arq destination ends up
        # re-downloading the same pack file once per blob inside it
        # — minutes instead of milliseconds. The cache is cleared
        # in ``__exit__`` so on-disk temp files don't leak past the
        # context manager. Maps remote path → local cached file.
        self._read_cache: Dict[str, Path] = {}

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
        # Drop the per-session pack-file cache so on-disk temps don't
        # leak past the context manager. Best-effort: a half-downloaded
        # file or a manually-removed cache entry shouldn't block
        # cleanup.
        for cached in list(self._read_cache.values()):
            try:
                cached.unlink()
            except OSError:
                pass
        self._read_cache.clear()
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
        return self._run_with_backoff(
            "ssh",
            lambda: subprocess.run(
                cmd, capture_output=True,
                timeout=(
                    timeout if timeout is not None
                    else self.op_timeout_sec
                ),
            ),
            command_summary=remote_cmd[:80],
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
            return self._run_with_backoff(
                "sftp",
                lambda: subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=(
                        timeout if timeout is not None
                        else self.op_timeout_sec
                    ),
                ),
                command_summary=batch_text.strip().splitlines()[0]
                if batch_text.strip() else "",
            )
        finally:
            try:
                Path(path).unlink()
            except OSError:
                pass

    def _run_with_backoff(
        self, kind: str,
        invoke: Callable[[], subprocess.CompletedProcess],
        *, command_summary: str = "",
    ) -> subprocess.CompletedProcess:
        """Execute ``invoke()`` and, if its stderr trips a
        rate-limit pattern, sleep + retry up to
        ``self._backoff.max_attempts`` times.

        On every retry we emit ``rate_limit_backoff`` on the
        backoff callback so the TUI can show "Throttling for X
        seconds…" — operators on Hetzner used to see opaque
        SftpRateLimitedError after 20 consecutive hits; now they
        see the throttling cycle and the run typically finishes
        without manual intervention.

        On final failure (max_attempts exhausted) we still call
        :meth:`_track_rate_limit` so the streak counter stays
        consistent with the legacy fail-fast semantics.
        """
        last_cp: Optional[subprocess.CompletedProcess] = None
        for attempt in range(self._backoff.max_attempts):
            sleep_s = self._backoff.sleep_for(attempt)
            if sleep_s > 0:
                self._notify_backoff(
                    sleep_sec=sleep_s, attempt=attempt,
                    kind=kind, command=command_summary,
                )
                time.sleep(sleep_s)
            cp = invoke()
            last_cp = cp
            stderr = cp.stderr
            if isinstance(stderr, (bytes, bytearray)):
                stderr_text = stderr.decode("utf-8", "replace")
            else:
                stderr_text = stderr or ""
            # Rate-limit pattern? Bump streak + maybe retry.
            matched = self._rate_limit.record(
                cp.returncode, stderr_text,
            )
            if cp.returncode == 0:
                return cp
            if matched is None:
                # Non-rate-limit failure — caller decides.
                return cp
            # Else: matched a rate-limit pattern. Loop tries the
            # next attempt with backoff. Don't raise on threshold
            # mid-loop so the retry actually gets attempted; the
            # threshold trip happens on the final pass below.
        # All retries exhausted — surface as the existing tracker
        # would have, so callers that catch SftpRateLimitedError
        # keep working.
        if last_cp is not None and self._rate_limit.threshold_hit():
            raise SftpRateLimitedError(
                f"SFTP backend hit rate-limit threshold after "
                f"{self._backoff.max_attempts} backoff attempts; "
                f"last pattern: "
                f"{self._rate_limit.last_pattern!r}"
            )
        return last_cp  # type: ignore[return-value]

    def _notify_backoff(
        self, *, sleep_sec: float, attempt: int, kind: str,
        command: str,
    ) -> None:
        if self._backoff_callback is None:
            return
        try:
            self._backoff_callback(
                "rate_limit_backoff",
                {"sleep_sec": float(sleep_sec),
                 "attempt": int(attempt),
                 "kind": kind,
                 "command": command,
                 "consecutive_failures":
                     self._rate_limit.consecutive_failures},
            )
        except Exception:
            pass

    def _track_rate_limit(
        self, cp: subprocess.CompletedProcess,
    ) -> None:
        """Feed the tracker with this command's stderr and raise
        :class:`SftpRateLimitedError` if the consecutive-failure
        threshold has been reached.

        Per-command stderr can be ``bytes`` (default for the
        binary-output ``_run_ssh`` path) or ``str`` (the
        text-mode ``_run_sftp_batch`` path); decode defensively
        so the same tracker handles both.
        """
        stderr = cp.stderr
        if isinstance(stderr, (bytes, bytearray)):
            stderr_text = stderr.decode("utf-8", "replace")
        else:
            stderr_text = stderr or ""
        self._rate_limit.record(cp.returncode, stderr_text)
        if self._rate_limit.threshold_hit():
            raise SftpRateLimitedError(
                f"SFTP backend hit rate-limit threshold "
                f"({self._rate_limit.consecutive_failures} consecutive "
                f"failures); last pattern matched: "
                f"{self._rate_limit.last_pattern!r}. Back off and "
                f"retry, or set rate_limit_abort_threshold higher "
                f"to suppress."
            )

    @property
    def rate_limit_failures(self) -> int:
        """Total number of rate-limit-shaped failures seen since
        construction (informational; the tracker also exposes
        ``consecutive_failures`` for the current streak)."""
        return self._rate_limit.total_failures

    @property
    def consecutive_rate_limit_failures(self) -> int:
        """Length of the current rate-limit failure streak. Resets
        to zero on the next successful command."""
        return self._rate_limit.consecutive_failures

    # ------------------------------------------------------------------
    # Path resolution (honors optional self.root prefix)
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        """Return the absolute server-side path for a backend-method
        argument. Empty ``root`` is the historical pass-through; a
        non-empty ``root`` prepends to leading-slash paths so callers
        can address blobs via root-relative POSIX strings."""
        if not self.root:
            return path
        if not path.startswith("/"):
            return path
        return self.root + path

    # ------------------------------------------------------------------
    # Backend protocol
    # ------------------------------------------------------------------

    def list_dir(self, path: str) -> List[str]:
        path = self._resolve(path)
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
        # Chrooted SFTP-only servers (Hetzner Storage Box, etc.) reject
        # arbitrary `ssh ... stat` commands — only the sftp protocol is
        # available. ``ls -l`` over sftp returns one long-format line
        # per matched path; the file size lives in column 5
        # (``-rw-r--r--    ?  user  group   <size>  Mon DD  YYYY  /full/path``).
        path = self._resolve(path)
        cp = self._run_sftp_batch(f"ls -l {shlex.quote(path)}\nbye\n")
        if cp.returncode != 0:
            raise RuntimeError(
                f"sftp ls -l {path}: rc={cp.returncode} "
                f"err={(cp.stderr or '').strip()[:300]}"
            )
        for line in (cp.stdout or "").splitlines():
            line = line.strip()
            if not line or line.startswith("sftp>"):
                continue
            cols = line.split()
            # Long-format minimum: perms links user group size date... name
            if len(cols) < 6:
                continue
            try:
                return int(cols[4])
            except ValueError:
                continue
        raise RuntimeError(
            f"sftp ls -l {path}: no size column parsed from "
            f"{(cp.stdout or '')[:300]!r}"
        )

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError(
                f"read_range bad args: offset={offset} length={length}"
            )
        if length == 0:
            return b""
        path = self._resolve(path)
        # Chrooted SFTP-only servers (Hetzner Storage Box etc.) reject
        # arbitrary ``ssh ... head -c`` / ``ssh ... dd`` commands. Try
        # the ssh path first because it's range-native (cheap on small
        # reads) — if the server rejects shell commands we fall back
        # to a full ``sftp get`` + in-memory slice. The fallback is
        # correct but downloads the whole file even for partial reads,
        # so callers reading byte ranges out of large pack files on
        # SFTP-only endpoints will pay the bandwidth cost.
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
        if cp.returncode == 0:
            return cp.stdout
        # SSH-side rejection (Hetzner-style chrooted endpoint, exit
        # code 8 / "Command not found"). Fall back to sftp ``get`` of
        # the whole file + in-memory slice. Correct for any range,
        # just bandwidth-wasteful when the file is large and we only
        # wanted a few bytes.
        return self._read_range_via_sftp_get(
            path, offset, length, timeout=timeout,
        )

    def _read_range_via_sftp_get(
        self, path: str, offset: int, length: int,
        *, timeout: int,
    ) -> bytes:
        """Whole-file fallback used when ``ssh head/dd`` is rejected.

        Caches the downloaded file in ``self._read_cache`` so
        subsequent range-reads against the same path (very common
        for pack files — one pack typically holds dozens of blobs)
        hit the local copy instead of re-downloading the whole
        thing. Cache entries are cleaned up in ``__exit__``.

        ``path`` must already be ``_resolve``-d (callers in this
        module pass the resolved path).
        """
        cached = self._read_cache.get(path)
        if cached is None or not cached.is_file():
            fd, tmp = tempfile.mkstemp(prefix="arq-sftp-cache-")
            os.close(fd)
            cached = Path(tmp)
            cp = self._run_sftp_batch(
                f"get {shlex.quote(path)} {shlex.quote(str(cached))}\nbye\n",
                timeout=timeout,
            )
            if cp.returncode != 0:
                # Best-effort cleanup of the half-downloaded file
                # before surfacing the failure.
                try:
                    cached.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"sftp get {path}: rc={cp.returncode} "
                    f"err={(cp.stderr or '').strip()[:300]}"
                )
            self._read_cache[path] = cached
        with open(cached, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def read_all(self, path: str) -> bytes:
        return self.read_range(path, 0, self.stat_size(path))

    def exists(self, path: str) -> bool:
        # sftp ``cd <path>`` succeeds for directories, fails for files
        # AND missing paths — so we additionally try ``ls -l <path>``
        # which succeeds for files. Either success → exists.
        path = self._resolve(path)
        cp_cd = self._run_sftp_batch(f"cd {shlex.quote(path)}\nbye\n")
        if cp_cd.returncode == 0:
            return True
        cp_ls = self._run_sftp_batch(f"ls -l {shlex.quote(path)}\nbye\n")
        return cp_ls.returncode == 0

    def is_dir(self, path: str) -> bool:
        # ``cd <path>`` is the cleanest directory probe over sftp:
        # rc=0 ⇒ directory, rc=1 ⇒ file or missing (stderr distinguishes
        # the two but :meth:`is_dir` only cares about the boolean).
        # Works on chrooted SFTP-only endpoints that reject arbitrary
        # ``ssh ... test -d`` commands.
        path = self._resolve(path)
        cp = self._run_sftp_batch(f"cd {shlex.quote(path)}\nbye\n")
        return cp.returncode == 0

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    def mkdir(
        self, path: str, *,
        parents: bool = True, exist_ok: bool = True,
    ) -> None:
        """Create ``path`` as a directory on the SFTP server.

        Issues ``mkdir`` commands through the sftp protocol so the
        method works on chrooted SFTP-only endpoints (Hetzner Storage
        Box, etc.) that reject arbitrary ``ssh ... mkdir`` shell
        commands. ``parents=True`` walks the path components and
        ``mkdir`` each one in turn; pre-existing directories are
        absorbed by ``exist_ok=True`` (the ``mkdir`` command's
        "Failure" stderr is treated as success when ``cd`` confirms
        the target exists as a directory).
        """
        if not exist_ok and self.exists(path):
            raise FileExistsError(f"sftp mkdir: already exists: {path}")
        resolved = self._resolve(path)
        if parents:
            # Build cumulative ancestor list so we can mkdir each one
            # in the right order. Skip any components that already
            # exist as directories (the ``cd`` probe is the same one
            # is_dir uses, so it works on Hetzner-style endpoints).
            parts = [p for p in resolved.split("/") if p]
            cur = "/" if resolved.startswith("/") else ""
            targets = []
            for part in parts:
                cur = (cur + part) if cur in ("", "/") else f"{cur}/{part}"
                cur_with_slash = cur if cur.startswith("/") else f"/{cur}"
                targets.append(cur_with_slash)
        else:
            targets = [resolved]
        for tgt in targets:
            # Skip if already a directory.
            cp_probe = self._run_sftp_batch(
                f"cd {shlex.quote(tgt)}\nbye\n",
            )
            if cp_probe.returncode == 0:
                continue
            cp = self._run_sftp_batch(
                f"mkdir {shlex.quote(tgt)}\nbye\n",
            )
            if cp.returncode != 0:
                # Tolerate "already exists" / race when exist_ok.
                if exist_ok:
                    cp_after = self._run_sftp_batch(
                        f"cd {shlex.quote(tgt)}\nbye\n",
                    )
                    if cp_after.returncode == 0:
                        continue
                raise RuntimeError(
                    f"sftp mkdir {tgt}: rc={cp.returncode} "
                    f"err={(cp.stderr or '').strip()[:300]}"
                )

    def write_all(self, path: str, data: bytes) -> None:
        """Atomically write ``data`` to ``path`` on the SFTP server.

        Implementation: write to a local temp file, ``sftp put`` it
        to a sibling ``<path>.partial.<pid>``, then ``rename`` to the
        final name. The ``put + rename`` pair makes a partial-write
        observable as the temp suffix rather than as a corrupt
        destination — the rename is atomic on POSIX filesystems.
        """
        self._require_open()
        path = self._resolve(path)
        fd, tmp_local = tempfile.mkstemp(prefix="arq-writer-put-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            # Stash under a sibling partial path so a crash mid-put
            # leaves the destination intact.
            partial = f"{path}.partial.{os.getpid()}"
            batch = (
                f"put {shlex.quote(tmp_local)} {shlex.quote(partial)}\n"
                f"rename {shlex.quote(partial)} {shlex.quote(path)}\n"
                f"bye\n"
            )
            cp = self._run_sftp_batch(batch)
            if cp.returncode != 0:
                err = (cp.stderr or "").strip()[:300]
                # Best-effort cleanup of the orphan partial. Use the
                # sftp protocol's own ``rm`` so chrooted endpoints
                # don't reject the shell-level rm we used to issue.
                try:
                    self._run_sftp_batch(
                        f"rm {shlex.quote(partial)}\nbye\n",
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    f"sftp put {path}: rc={cp.returncode} err={err}"
                )
        finally:
            try:
                os.unlink(tmp_local)
            except OSError:
                pass

    def unlink(self, path: str) -> None:
        """Remove ``path`` via the sftp protocol. Missing target =
        silent OK (matches ``rm -f`` semantics so a re-run is
        idempotent). Works on chrooted SFTP-only endpoints because
        we never reach for shell ``rm``."""
        self._require_open()
        path = self._resolve(path)
        cp = self._run_sftp_batch(f"rm {shlex.quote(path)}\nbye\n")
        if cp.returncode == 0:
            return
        err = (cp.stderr or "").strip()
        # Treat "no such file" as a no-op (rm -f semantics).
        # OpenSSH sftp returns rc=1 with "No such file or directory"
        # in stderr when the target is missing.
        if "No such file" in err or "no such file" in err:
            return
        raise RuntimeError(
            f"sftp unlink {path}: rc={cp.returncode} err={err[:300]}"
        )
