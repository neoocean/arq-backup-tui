"""macOS default exclusion patterns matching Arq.app v8.

Arq.app excludes a documented set of paths by default —
``~/Library/Caches/``, ``~/.Trash``, ``/private/var/folders``, etc.
These paths are either:

- **OS-managed caches** that change rapidly + don't carry
  irreplaceable user data
- **System-internal state** the OS rebuilds on demand
- **Trash** the operator already marked for deletion

Including them inflates backup size 5-50× without adding
restoration value. Arq.app's GUI surfaces this list as
"Excluded by Default" toggles per plan.

Convention sources:

- Arq.app v8 plan defaults (sampled from operator documentation)
- ``man tmutil(8)`` system exclusion list
- Apple's `/System/Library/CoreServices/backupd.bundle` plist
  (default-backup exclude lists)

The :func:`macos_default_excludes` helper returns the canonical
list as an :class:`~arq_writer.exclusions.ExclusionRules` so it
can be passed straight to ``Backup`` / ``build_backup`` via the
``exclusions`` kwarg, or merged with operator-supplied rules.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .exclusions import ExclusionRules


# Per-user paths that should never be backed up by default.
# Wildcard format: relative-path-from-source-root globs.
MACOS_USER_DEFAULT_EXCLUDES = (
    # Browser + OS cache directories — rapidly-mutating, no
    # backup value.
    "Library/Caches",
    "Library/Caches/*",
    "Library/Caches/**/*",
    # Trash — the operator has marked these for deletion.
    ".Trash",
    ".Trash/*",
    ".Trash/**/*",
    # iCloud staging dirs.
    "Library/Mobile Documents/com~apple~CloudDocs/.icloud",
    # Spotlight + system metadata stores.
    "Library/Metadata/CoreSpotlight",
    "Library/Metadata/CoreSpotlight/*",
    "Library/Metadata/CoreSpotlight/**/*",
    # Application support sandbox containers — Arq excludes these
    # because their contents are app-managed state that
    # individual apps restore themselves.
    "Library/Containers/*/Data/Library/Caches",
    "Library/Containers/*/Data/Library/Caches/**/*",
    # Time Machine itself (don't back up the backup).
    "Library/Application Support/MobileSync",
    # IDE / build-tool cache piles.
    "Library/Developer/Xcode/DerivedData",
    "Library/Developer/Xcode/DerivedData/*",
    "Library/Developer/Xcode/DerivedData/**/*",
    "Library/Developer/CoreSimulator/Caches",
    ".npm/_cacache",
    ".npm/_cacache/**/*",
    ".cache",
    ".cache/*",
    ".cache/**/*",
)

# System-wide paths (rooted at ``/``) that should never be backed
# up. These only apply when the operator's source includes the
# system root.
MACOS_SYSTEM_DEFAULT_EXCLUDES = (
    # Per-user/system temp directories.
    "private/var/folders",
    "private/var/folders/*",
    "private/var/folders/**/*",
    "private/tmp",
    "private/tmp/*",
    "private/tmp/**/*",
    "tmp",
    "tmp/*",
    "tmp/**/*",
    # Active swap / VM.
    "private/var/vm",
    "private/var/vm/*",
    # System logs (regenerated continuously).
    "private/var/log",
    "private/var/log/*",
    "private/var/log/**/*",
    # Spotlight rebuild target.
    ".Spotlight-V100",
    ".Spotlight-V100/*",
    ".Spotlight-V100/**/*",
    # fseventsd state.
    ".fseventsd",
    ".fseventsd/*",
    ".fseventsd/**/*",
    # APFS volume-internal metadata.
    ".DocumentRevisions-V100",
    ".DocumentRevisions-V100/*",
    ".DocumentRevisions-V100/**/*",
    ".HFS+ Private Directory Data",
    ".HFS+ Private Directory Data\r",
)


def macos_default_excludes(
    *,
    include_user: bool = True,
    include_system: bool = True,
    extra_wildcards: Optional[Sequence[str]] = None,
) -> ExclusionRules:
    """Build an :class:`ExclusionRules` covering the macOS default
    skip list Arq.app uses.

    ``include_user`` controls the per-user paths
    (Library/Caches etc.); enable when backing up a home dir.
    ``include_system`` controls the system-wide paths
    (/private/var/folders etc.); enable when the source covers
    the boot volume root.

    ``extra_wildcards`` appends operator-supplied globs on top
    of the defaults; pass through any plan-specific exclusions.
    """
    patterns = []
    if include_user:
        patterns.extend(MACOS_USER_DEFAULT_EXCLUDES)
    if include_system:
        patterns.extend(MACOS_SYSTEM_DEFAULT_EXCLUDES)
    if extra_wildcards:
        patterns.extend(extra_wildcards)
    return ExclusionRules.of(wildcard=patterns)
