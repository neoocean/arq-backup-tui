"""Integration tests against real Arq 7 destinations.

These tests are **opt-in only** — they auto-skip whenever the
required environment variables aren't set. CI runs them as no-ops;
a developer with credentials gets full execution.

See ``docs/COMPAT-SFTP-TESTING.md`` for the env-var contract and
the credential-paste workflow.
"""
