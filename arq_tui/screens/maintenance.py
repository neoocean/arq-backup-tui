"""Maintenance actions on an existing destination.

Two operator-facing housekeeping tasks live here, both routed
through a single :class:`MaintenanceScreen` reachable from the
backup-set browser:

- **Rotate keyset password** — re-encrypt
  ``<computer-uuid>/encryptedkeyset.dat`` under a new password while
  keeping the underlying ``(encryption_key, hmac_key, blob_id_salt)``
  triple intact. Existing backuprecords / blobs continue to decrypt
  afterward; the new password is what's required to unlock the
  keyset on subsequent runs. The superseded keyset is archived to
  ``keyset_history/encryptedkeyset_<unix-epoch>.dat`` exactly as
  Arq.app does, via ``rotate_keyset_password_on_disk``.
- **Apply retention** — call :func:`arq_writer.apply_retention`
  with a small policy form, optionally as a dry run. The on-disk
  effects (deleted record / standalone blob / pack counts) stream
  back as TUI log lines.

Both flows reuse the destination's already-open backend + cached
encryption password so the operator doesn't re-enter credentials
mid-session.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    RadioButton,
    RadioSet,
    Static,
)

from ..state import Destination
from ._overlay import OverlayScreen


class MaintenanceScreen(OverlayScreen):
    """Single-pane maintenance console for one open destination."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
    ]

    DEFAULT_CSS = """
    MaintenanceScreen #container {
        padding: 0;
        height: auto;
    }
    MaintenanceScreen .section-title {
        text-style: bold;
        margin-top: 1;
    }
    MaintenanceScreen .field-label {
        color: $text-muted;
        margin-top: 1;
    }
    MaintenanceScreen #log {
        margin-top: 1;
        border: round $primary;
        padding: 0 1;
        height: 6;
    }
    MaintenanceScreen .button-row {
        height: 3;
        align: left middle;
        margin-top: 1;
    }
    MaintenanceScreen .button-row Button {
        margin: 0 1 0 0;
    }
    """

    def __init__(
        self, *,
        backend,
        dest: Destination,
        password: str,
        computer_uuid: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._dest = dest
        self._password = password
        self._computer_uuid = computer_uuid
        self._log_lines: list = []
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="container", classes="overlay-box"):
            yield Static(
                f"Maintenance: {self._dest.display()}",
                classes="section-title",
            )
            yield Label(
                "Rotate keyset password", classes="section-title",
            )
            yield Label("Current password (defaults to cached)",
                        classes="field-label")
            yield Input(
                password=True, id="rot-old-password",
                placeholder="(uses cached password if blank)",
            )
            yield Label("New password", classes="field-label")
            yield Input(password=True, id="rot-new-password")
            yield Label("Confirm new password", classes="field-label")
            yield Input(password=True, id="rot-new-password2")
            with Horizontal(classes="button-row"):
                yield Button(
                    "Rotate password",
                    id="btn-rotate", variant="primary",
                )
            yield Label(
                "Apply retention", classes="section-title",
            )
            yield Label("keep_last_n", classes="field-label")
            yield Input(id="ret-keep-last-n")
            yield Label("keep_daily", classes="field-label")
            yield Input(id="ret-keep-daily", placeholder="0")
            yield Label("keep_weekly", classes="field-label")
            yield Input(id="ret-keep-weekly", placeholder="0")
            yield Label("keep_monthly", classes="field-label")
            yield Input(id="ret-keep-monthly", placeholder="0")
            yield Label("keep_yearly", classes="field-label")
            yield Input(id="ret-keep-yearly", placeholder="0")
            yield Label("Run mode", classes="field-label")
            with RadioSet(id="ret-mode-set"):
                yield RadioButton(
                    "Dry run (preview only)", value=True,
                    id="ret-mode-dry",
                )
                yield RadioButton("Real run", id="ret-mode-real")
            yield Label("Run blob GC after pruning",
                        classes="field-label")
            with RadioSet(id="ret-gc-set"):
                yield RadioButton("Yes", value=True, id="ret-gc-on")
                yield RadioButton("No", id="ret-gc-off")
            with Horizontal(classes="button-row"):
                yield Button(
                    "Apply retention",
                    id="btn-retention", variant="primary",
                )
            yield Static("", id="log")
        yield Footer()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        self._log_lines.append(msg)
        # Keep the log bounded — the panel is informational, not a
        # full transcript.
        if len(self._log_lines) > 200:
            self._log_lines = self._log_lines[-200:]
        self.query_one("#log", Static).update("\n".join(self._log_lines))

    # ------------------------------------------------------------------
    # Button dispatch
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._busy:
            self.notify(
                "A maintenance task is already running.",
                severity="warning",
            )
            return
        if event.button.id == "btn-rotate":
            self._start_rotate()
        elif event.button.id == "btn-retention":
            self._start_retention()

    # ------------------------------------------------------------------
    # Password rotation
    # ------------------------------------------------------------------

    def _start_rotate(self) -> None:
        old = self.query_one(
            "#rot-old-password", Input,
        ).value or self._password
        new1 = self.query_one("#rot-new-password", Input).value
        new2 = self.query_one("#rot-new-password2", Input).value
        if not new1:
            self.notify("Enter a new password.", severity="error")
            return
        if new1 != new2:
            self.notify(
                "New passwords don't match.", severity="error",
            )
            return
        if not old:
            self.notify(
                "Old password not available.", severity="error",
            )
            return
        cuuid = self._computer_uuid or self._discover_computer_uuid()
        if not cuuid:
            self.notify(
                "Could not locate computer UUID under destination.",
                severity="error",
            )
            return
        self._busy = True
        self._log(f"Rotating keyset password under {cuuid} …")
        # Run rotation in a worker thread to keep the UI responsive
        # even though rotation itself is fast (one PBKDF2 pair).
        thread = threading.Thread(
            target=self._rotate_blocking,
            args=(cuuid, old, new1),
            daemon=True,
        )
        thread.start()

    def _rotate_blocking(
        self, cuuid: str, old_password: str, new_password: str,
    ) -> None:
        from arq_writer import rotate_keyset_password_on_disk
        try:
            # Archives the superseded keyset to keyset_history/ exactly
            # as Arq.app does on a GUI password change (full parity).
            rotate_keyset_password_on_disk(
                self._backend,
                cuuid,
                old_password=old_password,
                new_password=new_password,
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure
            self.app.call_from_thread(
                self._on_rotate_failed, exc,
            )
            return
        self.app.call_from_thread(self._on_rotate_done, new_password)

    def _on_rotate_done(self, new_password: str) -> None:
        self._busy = False
        self._password = new_password
        self.app.credential_cache.set_encryption_password(
            self._dest, new_password,
        )
        self._log("Rotation complete; new password cached.")
        self.notify(
            "Password rotated; cached for this session.",
            severity="information",
        )
        # Clear the password fields so the plaintext doesn't sit
        # in the widget tree.
        for wid in (
            "#rot-old-password", "#rot-new-password",
            "#rot-new-password2",
        ):
            self.query_one(wid, Input).value = ""

    def _on_rotate_failed(self, exc: Exception) -> None:
        self._busy = False
        self._log(f"Rotation FAILED: {type(exc).__name__}: {exc}")
        self.notify(
            f"Rotation failed: {exc}", severity="error",
        )

    def _discover_computer_uuid(self) -> Optional[str]:
        """Find the single computer UUID directory under the
        destination root by listing entries that look like UUIDs.
        Returns ``None`` if the root has zero or more than one,
        since the operator must pick explicitly in that case."""
        import re
        try:
            entries = self._backend.list_dir("/")
        except Exception:
            return None
        uuid_pat = re.compile(
            r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-"
            r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$",
        )
        candidates = [
            e for e in entries if uuid_pat.match(e)
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    def _read_int_or_none(self, widget_id: str) -> Any:
        text = self.query_one(widget_id, Input).value.strip()
        if not text:
            return None
        try:
            v = int(text)
            if v < 0:
                raise ValueError("negative")
        except ValueError:
            self.notify(
                f"{widget_id} must be a non-negative integer.",
                severity="error",
            )
            return "INVALID"
        return v

    def _start_retention(self) -> None:
        cuuid = self._computer_uuid or self._discover_computer_uuid()
        if not cuuid:
            self.notify(
                "Could not locate computer UUID under destination.",
                severity="error",
            )
            return
        ret_kwargs = {}
        # Translate UI fields → policy kwargs. Empty inputs become
        # 0 so the policy "won't keep this bucket" rather than
        # "keep everything in this bucket".
        for arg_name, widget_id in (
            ("keep_last_n", "#ret-keep-last-n"),
            ("keep_daily", "#ret-keep-daily"),
            ("keep_weekly", "#ret-keep-weekly"),
            ("keep_monthly", "#ret-keep-monthly"),
            ("keep_yearly", "#ret-keep-yearly"),
        ):
            v = self._read_int_or_none(widget_id)
            if v == "INVALID":
                return
            if v is None:
                continue
            ret_kwargs[arg_name] = v
        if not ret_kwargs:
            self.notify(
                "Set at least one keep_* field.",
                severity="warning",
            )
            return
        dry_run = self.query_one(
            "#ret-mode-dry", RadioButton,
        ).value
        run_gc = self.query_one(
            "#ret-gc-on", RadioButton,
        ).value
        self._busy = True
        mode = "dry-run" if dry_run else "REAL"
        self._log(
            f"Applying retention ({mode}, gc={'on' if run_gc else 'off'}) "
            f"under {cuuid}: "
            + ", ".join(f"{k}={v}" for k, v in sorted(ret_kwargs.items()))
        )
        thread = threading.Thread(
            target=self._retention_blocking,
            kwargs={
                "cuuid": cuuid,
                "ret_kwargs": ret_kwargs,
                "dry_run": dry_run,
                "run_gc": run_gc,
            },
            daemon=True,
        )
        thread.start()

    def _retention_blocking(
        self, *,
        cuuid: str, ret_kwargs: dict,
        dry_run: bool, run_gc: bool,
    ) -> None:
        from arq_writer import RetentionPolicy, apply_retention
        try:
            policy = RetentionPolicy(**ret_kwargs)
            result = apply_retention(
                self._backend,
                encryption_password=self._password,
                policy=policy,
                computer_uuid=cuuid,
                run_gc=run_gc,
                dry_run=dry_run,
                on_event=self._retention_event,
            )
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(
                self._on_retention_failed, exc,
            )
            return
        self.app.call_from_thread(self._on_retention_done, result)

    def _retention_event(self, kind: str, payload: dict) -> None:
        # Routed through call_from_thread because the worker
        # thread invokes the callback directly.
        self.app.call_from_thread(
            self._log,
            f"  · {kind}: "
            + ", ".join(
                f"{k}={v}" for k, v in sorted(payload.items())
            ),
        )

    def _on_retention_done(self, result) -> None:
        self._busy = False
        prune = result.prune
        gc = result.gc
        line = (
            f"prune: deleted={prune.deleted_count} "
            f"retained={prune.retained_count}"
        )
        if gc is not None:
            line += (
                f"; gc: blobs={gc.deleted_blobs} packs={gc.deleted_packs}"
            )
        self._log("Retention complete.")
        self._log(line)
        self.notify(line, severity="information")

    def _on_retention_failed(self, exc: Exception) -> None:
        self._busy = False
        self._log(f"Retention FAILED: {type(exc).__name__}: {exc}")
        self.notify(
            f"Retention failed: {exc}", severity="error",
        )
