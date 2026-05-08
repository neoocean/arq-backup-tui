"""Source-tree exclusion rules.

Three filter shapes — each consulted by ``Backup._walk_dir`` for
every entry it considers:

- **Wildcard / glob** (``*.log``, ``__pycache__``, ``node_modules``).
  Uses :mod:`fnmatch` semantics: ``*`` matches anything except
  ``/``, ``?`` matches a single character, ``[abc]`` matches one
  of, etc. Matched against both the entry name AND the full
  source-relative POSIX path.

- **Regex** (``r"\.tmp$"`` etc.). Each pattern is precompiled and
  matched against the source-relative path. Use ``re.fullmatch`` —
  callers writing partial-match patterns must anchor with ``.*`` /
  ``.+`` themselves.

- **.gitignore-style** — minimal subset:
  - ``# comment`` lines and blank lines are ignored
  - ``foo`` matches a basename anywhere in the tree
  - ``/foo`` matches a path relative to the source root
  - ``foo/`` matches a directory only
  - ``*.ext`` — wildcard (delegates to fnmatch)
  - ``!pattern`` re-includes a previously-excluded path (negation
    is processed in source order)

  The full git semantics (``**``, character ranges across
  separators, etc.) is **not** implemented — use the
  ``regex_excludes`` field for anything more specific.

The :class:`ExclusionRules` instance is immutable; callers
construct one and pass it to the writer. Empty rule set = no
exclusion (all entries pass).

Maximum-file-size limits are a separate concern handled inline by
the writer's ``_walk_file``; see ``Backup(max_file_bytes=...)``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ExclusionRules:
    """Bundle of exclusion patterns. All fields default to empty."""

    wildcard: Tuple[str, ...] = ()
    regex: Tuple[str, ...] = ()
    gitignore_lines: Tuple[str, ...] = ()
    _compiled_regex: Tuple = field(default=(), repr=False, compare=False)
    _gitignore_rules: Tuple = field(default=(), repr=False, compare=False)

    @classmethod
    def empty(cls) -> "ExclusionRules":
        return cls()

    @classmethod
    def of(
        cls,
        *,
        wildcard: Optional[Sequence[str]] = None,
        regex: Optional[Sequence[str]] = None,
        gitignore_lines: Optional[Sequence[str]] = None,
    ) -> "ExclusionRules":
        wc = tuple(wildcard or ())
        rx = tuple(regex or ())
        gi = tuple(gitignore_lines or ())
        compiled = tuple(re.compile(p) for p in rx)
        gitignore_rules = tuple(_parse_gitignore(gi))
        return cls(
            wildcard=wc,
            regex=rx,
            gitignore_lines=gi,
            _compiled_regex=compiled,
            _gitignore_rules=gitignore_rules,
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.wildcard or self._compiled_regex or self._gitignore_rules
        )

    def excludes(self, rel_path: str, *, is_dir: bool) -> bool:
        """Return True iff ``rel_path`` should be skipped.

        ``rel_path`` is the source-relative POSIX path (no leading
        slash). For a path under multiple matching rules the
        outcome is determined by:

        1. wildcard / regex (any match → exclude)
        2. gitignore rules (last-matching rule wins, with negation)
        """
        if self.is_empty:
            return False
        rel_path = rel_path.lstrip("/")
        name = rel_path.rsplit("/", 1)[-1]
        # 1. wildcard
        for pat in self.wildcard:
            if fnmatch.fnmatchcase(name, pat):
                return True
            if fnmatch.fnmatchcase(rel_path, pat):
                return True
        # 2. regex
        for pat in self._compiled_regex:
            if pat.fullmatch(rel_path):
                return True
        # 3. gitignore (last-match-wins)
        excluded = False
        for rule in self._gitignore_rules:
            if rule.matches(rel_path, is_dir=is_dir):
                excluded = not rule.negated
        return excluded


# ---------------------------------------------------------------------------
# .gitignore parser (minimal subset)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GitignoreRule:
    pattern: str
    negated: bool
    anchored: bool       # True iff pattern starts with /
    dir_only: bool       # True iff pattern ends with /

    def matches(self, rel_path: str, *, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        # If anchored, match against the full rel_path; otherwise
        # match either basename or any rel-path suffix.
        candidates = (
            (rel_path,) if self.anchored
            else (
                rel_path,
                rel_path.rsplit("/", 1)[-1],
                *_path_suffixes(rel_path),
            )
        )
        for cand in candidates:
            if fnmatch.fnmatchcase(cand, self.pattern):
                return True
        return False


def _path_suffixes(rel_path: str) -> Sequence[str]:
    """Yield every ``a/b/c`` suffix of ``rel_path``."""
    parts = rel_path.split("/")
    return tuple("/".join(parts[i:]) for i in range(1, len(parts)))


def _parse_gitignore(lines: Sequence[str]) -> List[_GitignoreRule]:
    rules: List[_GitignoreRule] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].lstrip()
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        if not line:
            continue
        rules.append(_GitignoreRule(
            pattern=line,
            negated=negated,
            anchored=anchored,
            dir_only=dir_only,
        ))
    return rules
