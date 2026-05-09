#!/usr/bin/env python3
"""Generate ``CHANGELOG.md`` from the git history.

Scans every commit on the current branch, groups by an
operator-friendly category derived from the first line, and
emits a chronologically-ordered Markdown changelog. Designed
to be re-runnable: each invocation overwrites ``CHANGELOG.md``
verbatim, so the file is always a faithful render of the
commit graph.

Categories:

- **Features**: commit subjects starting with ``feat:``,
  ``Feature``, ``Group N`` (matches the PR-bundle pattern
  this project uses for grouped feature batches), or
  containing ``adds`` / ``new`` near the front.
- **Fixes**: ``fix:``, ``Fix CI``, ``hotfix``, ``patch``.
- **Docs**: ``docs:``, anything touching only ``*.md``.
- **Tests**: commits whose changeset is purely under
  ``tests/``.
- **Internal**: refactors, dependency bumps, anything else.

Each entry is ``- <subject> (#<PR>) [<sha7>]`` so the operator
can click through to the PR for the full body.

Usage::

    python3 scripts/build_changelog.py [--out CHANGELOG.md]
                                        [--since vX.Y.Z]

Run without args to regenerate the entire history; with
``--since`` to only include commits after a given tag /
ref. The output overwrites ``--out`` (default
``CHANGELOG.md``).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class CommitEntry:
    sha7: str
    subject: str
    author_date: str        # YYYY-MM-DD
    pr_number: Optional[str] = None
    is_merge: bool = False
    files_changed: List[str] = field(default_factory=list)


# Category detection. Patterns checked in order; first match
# wins. ``files`` predicate, when present, forces a category
# based on the changeset alone.
_FEATURES = re.compile(
    r"^(feat|feature|group \d|adds?\b|new\b)",
    re.IGNORECASE,
)
_FIXES = re.compile(
    r"^(fix|hotfix|patch|bug)", re.IGNORECASE,
)
_DOCS = re.compile(r"^(docs?|readme|notes?)", re.IGNORECASE)


def _categorise(entry: CommitEntry) -> str:
    """Bucket name for ``entry``. See module docstring for the
    rules."""
    s = entry.subject
    if _FEATURES.match(s):
        return "Features"
    if _FIXES.match(s):
        return "Fixes"
    if _DOCS.match(s):
        return "Docs"
    # Files-only checks come second so a "fix: ..." that
    # happens to touch only .md still lands in Fixes.
    if entry.files_changed and all(
        f.endswith(".md") for f in entry.files_changed
    ):
        return "Docs"
    if entry.files_changed and all(
        f.startswith("tests/") for f in entry.files_changed
    ):
        return "Tests"
    return "Internal"


# ---------------------------------------------------------------------------
# Git driver
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    cp = subprocess.run(
        ["git", *args],
        capture_output=True, text=True, check=True,
    )
    return cp.stdout


def _collect_commits(since: Optional[str]) -> List[CommitEntry]:
    """Walk git log → CommitEntry list, newest first."""
    rev_range = f"{since}..HEAD" if since else "HEAD"
    # %H = full sha, %h = short, %s = subject, %as = author date
    log = _git(
        "log", rev_range, "--no-merges",
        "--pretty=format:%h\t%s\t%as",
    )
    out: List[CommitEntry] = []
    pr_re = re.compile(r"\(#(\d+)\)$")
    for line in log.splitlines():
        if not line.strip():
            continue
        try:
            sha7, subject, author_date = line.split("\t", 2)
        except ValueError:
            continue
        m = pr_re.search(subject)
        pr = m.group(1) if m else None
        # Strip the "(#N)" suffix from the rendered subject so
        # the entry doesn't double up the PR number.
        subj_clean = pr_re.sub("", subject).rstrip()
        # Pull file list per commit. Each call is one git invoke
        # (cheap on local repos; slow on huge histories — pass
        # --since to scope down then).
        try:
            files_text = _git(
                "diff-tree", "--no-commit-id", "--name-only",
                "-r", sha7,
            )
            files = [
                f for f in files_text.splitlines() if f.strip()
            ]
        except subprocess.CalledProcessError:
            files = []
        out.append(CommitEntry(
            sha7=sha7, subject=subj_clean,
            author_date=author_date,
            pr_number=pr,
            files_changed=files,
        ))
    return out


# ---------------------------------------------------------------------------
# Markdown emitter
# ---------------------------------------------------------------------------


def render_changelog(
    commits: List[CommitEntry],
    *,
    title: str = "Changelog",
    repo_url: Optional[str] = None,
) -> str:
    """Render ``commits`` (newest-first) into the Markdown body
    we write to CHANGELOG.md.

    ``repo_url`` (e.g. ``https://github.com/neoocean/arq-backup-tui``)
    turns each PR / SHA reference into a clickable link. Pass
    ``None`` to keep the entries plain text.
    """
    if not commits:
        return f"# {title}\n\nNo changes.\n"
    # Group by date (YYYY-MM) so sections aren't enormous.
    by_month: "OrderedDict[str, Dict[str, List[CommitEntry]]]" = (
        OrderedDict()
    )
    for c in commits:
        month = c.author_date[:7] if c.author_date else "0000-00"
        by_month.setdefault(month, OrderedDict())
        cat = _categorise(c)
        by_month[month].setdefault(cat, []).append(c)

    lines: List[str] = [f"# {title}\n"]
    lines.append(
        "Auto-generated from the git log by "
        "`scripts/build_changelog.py`. Each entry shows the "
        "commit subject + (PR if available) + short SHA. "
        "Re-run the script to regenerate after merging more "
        "PRs.\n"
    )
    for month, by_cat in by_month.items():
        lines.append(f"## {month}\n")
        # Stable category order, then commits within each.
        for cat in ("Features", "Fixes", "Docs",
                    "Tests", "Internal"):
            if cat not in by_cat:
                continue
            lines.append(f"### {cat}\n")
            for c in by_cat[cat]:
                lines.append(_render_one(c, repo_url))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_one(c: CommitEntry, repo_url: Optional[str]) -> str:
    if repo_url:
        sha_link = f"[{c.sha7}]({repo_url}/commit/{c.sha7})"
        if c.pr_number:
            pr_link = (
                f"[#{c.pr_number}]({repo_url}/pull/{c.pr_number})"
            )
            return (
                f"- {c.subject} ({pr_link}) {sha_link}"
            )
        return f"- {c.subject} {sha_link}"
    if c.pr_number:
        return f"- {c.subject} (#{c.pr_number}) [{c.sha7}]"
    return f"- {c.subject} [{c.sha7}]"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--out", type=Path,
        default=Path(__file__).resolve().parent.parent
                / "CHANGELOG.md",
    )
    p.add_argument("--since", default=None)
    p.add_argument(
        "--repo-url", default=(
            "https://github.com/neoocean/arq-backup-tui"
        ),
    )
    args = p.parse_args(argv)
    commits = _collect_commits(args.since)
    body = render_changelog(commits, repo_url=args.repo_url)
    args.out.write_text(body, encoding="utf-8")
    print(
        f"wrote {args.out} ({len(commits)} commits)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
