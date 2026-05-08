# `.secrets/` — operator credentials for live destination tests

This directory holds the credentials the integration tests
(`tests/integration/test_arqapp_sftp_compat.py`,
`tests/integration/test_arq_real_destination.py`) read when the
operator wants to verify the **reader / validator / writer** stack
against a real Arq.app-managed SFTP destination.

The actual credential files (`sftp.json`, `dest_password`) are
**never** committed to git or submitted to Perforce — both
`.gitignore` and `.p4ignore` keep them local. Only this README and
the two `.example` files are tracked, so the expected layout
stays self-documenting.

## Layout

```
.secrets/
├── README.md                  ← committed, this file
├── sftp.json.example          ← committed, template
├── dest_password.example      ← committed, template
├── sftp.json                  ← LOCAL ONLY, copy of .example with real values
└── dest_password              ← LOCAL ONLY, the Arq encryption password
```

## Setup

```sh
cp .secrets/sftp.json.example      .secrets/sftp.json
cp .secrets/dest_password.example  .secrets/dest_password
$EDITOR .secrets/sftp.json
$EDITOR .secrets/dest_password
chmod 600 .secrets/sftp.json .secrets/dest_password
```

The integration tests auto-discover them — no env-var wiring is
required when `.secrets/` is populated. Resolution order is
`.secrets/` > `.env` > `os.environ`, so a `.secrets/` value always
wins over a stale env var.

## What gets verified

Once `.secrets/` is in place, the integration suite covers the
full read + validate + write surface against the real destination:

- **Reader**: discover layout (computer-uuid → folder-uuid →
  records), restore the latest record of every folder to a
  scratch directory, sample-verify file content + tree shape.
- **Validator**: run L0 / L1a / L1b tiers in full plus a bytes-
  capped L2 audit-drip; check Arq 7 format conformance.
- **Writer**: write a small synthetic backup into the
  `write_subdir` (defaults to `.arq-backup-tui-write-test/` —
  dot-prefixed and *not* UUID-shaped, so it lives alongside the
  operator's real Arq.app destinations without colliding). Then
  read that synthetic backup back through the reader and run the
  validator against it. The operator's real backup data is **never
  written to** — only its keyset password is reused so the
  synthetic backup decrypts under the same credentials.

If any required field is missing, every integration test
auto-skips with a clear reason; default test runs on machines
without `.secrets/` continue to pass.

## Security notes

- `chmod 600` so only the local user can read.
- Passwords with spaces / special characters work — the file
  parser strips at most one trailing newline and preserves
  everything else byte-for-byte.
- A leaked `.secrets/dest_password` lets an attacker decrypt the
  destination if they also have the SFTP credentials. Treat it
  like the keyring password it is.
- Rotate via Arq.app GUI (or the `MaintenanceScreen` in this
  TUI) and update `.secrets/dest_password` afterward.

For the legacy `.env` workflow (the one PR #9 introduced), see
`docs/COMPAT-SFTP-TESTING.md`. The two are interchangeable; the
loader merges them transparently.
