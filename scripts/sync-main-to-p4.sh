#!/bin/bash
# Replay every commit on `main` newer than the last one already
# submitted to Perforce. The cursor is the last line of
# `.p4-git-sync-log` (which doubles as the per-CL marker we
# append on every iteration), so the script is idempotent —
# re-running with `main` unchanged is a no-op.
#
# Triggered automatically by:
#   .git/hooks/post-merge     git pull / git merge brought in commits
#   .git/hooks/post-commit    direct local commit on main
#   .git/hooks/post-rewrite   rebase / amend changed history on main
#
# Or manually:
#   ./scripts/sync-main-to-p4.sh
#
# Designed to never block git operations:
#   - exits 0 silently when p4 isn't installed (e.g. CI / Linux dev)
#   - exits 0 silently when not logged in to p4
#   - exits 0 silently when another sync is already running
#   - restores the original branch on exit (success OR failure)

set -e
set -o pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPOT_PATH="//woojinkim/scripts/arq-backup-tui/..."
LOG_FILE="$PROJECT_DIR/.p4-git-sync-log"
LOCK_FILE="$PROJECT_DIR/.p4-git-sync.lock"
PROGRESS_PREFIX="[sync-main-to-p4]"

cd "$PROJECT_DIR"
export P4IGNORE="$PROJECT_DIR/.p4ignore"

# ---- Pre-flight: skip silently on non-p4 / non-logged-in machines ----

if ! command -v p4 >/dev/null 2>&1; then
    exit 0
fi
if ! p4 info >/dev/null 2>&1; then
    # No p4 client config (e.g. running outside the surface
    # workspace). Don't block the git operation.
    exit 0
fi
if ! p4 login -s >/dev/null 2>&1; then
    echo "$PROGRESS_PREFIX p4 not logged in; skipping" >&2
    exit 0
fi

# ---- PID-based lockfile (macOS portable; flock(1) is Linux-only) ----

if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "$PROGRESS_PREFIX another sync is in progress (pid $OLD_PID); skipping" >&2
        exit 0
    fi
    # Stale lock — owner is gone. Reclaim it.
fi
echo $$ > "$LOCK_FILE"

cleanup_lock() {
    rm -f "$LOCK_FILE"
}
trap cleanup_lock EXIT

# ---- Determine cursor + target ----

if ! git rev-parse main >/dev/null 2>&1; then
    # No main branch — nothing to sync.
    exit 0
fi
TARGET_SHA=$(git rev-parse main)

if [ -f "$LOG_FILE" ]; then
    LAST_SHA=$(awk '{print $1}' "$LOG_FILE" | tail -1)
else
    LAST_SHA=""
fi

if [ "$LAST_SHA" = "$TARGET_SHA" ]; then
    # Already in sync.
    exit 0
fi

if [ -n "$LAST_SHA" ]; then
    if ! git rev-parse "$LAST_SHA" >/dev/null 2>&1; then
        echo "$PROGRESS_PREFIX cursor $LAST_SHA not in this clone; refusing to guess" >&2
        echo "$PROGRESS_PREFIX run scripts/replay-git-to-p4.sh to do a full re-replay if intended" >&2
        exit 1
    fi
    COMMITS=$(git log "$LAST_SHA..main" --reverse --pretty=format:"%H")
else
    # No prior cursor — replay everything on main from scratch.
    # This is the bootstrap case (handled by the original
    # one-shot replay; running this branch unattended would be
    # a 57-CL flood, so refuse and tell the operator).
    echo "$PROGRESS_PREFIX no $LOG_FILE — refusing to bootstrap automatically" >&2
    echo "$PROGRESS_PREFIX run scripts/replay-git-to-p4.sh once to seed the cursor" >&2
    exit 1
fi

if [ -z "$COMMITS" ]; then
    exit 0
fi

# ---- Replay loop ----

ORIG_BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$ORIG_BRANCH" = "HEAD" ] && ORIG_BRANCH=$(git rev-parse HEAD)

restore_branch() {
    git checkout -f "$ORIG_BRANCH" >/dev/null 2>&1 || true
    cleanup_lock
}
trap restore_branch EXIT

TOTAL=$(echo "$COMMITS" | wc -l | tr -d ' ')
echo "$PROGRESS_PREFIX replaying $TOTAL new commit(s) on main → $DEPOT_PATH"

i=0
FAILED=0
for sha in $COMMITS; do
    i=$((i+1))
    subject=$(git log -1 --pretty=format:"%s" "$sha")
    body=$(git log -1 --pretty=format:"%b" "$sha")
    author=$(git log -1 --pretty=format:"%an <%ae>" "$sha")
    cdate=$(git log -1 --pretty=format:"%aI" "$sha")

    printf "%s [%d/%d] %s %s\n" "$PROGRESS_PREFIX" "$i" "$TOTAL" "$sha" "$subject"

    git -c advice.detachedHead=false checkout -f "$sha" >/dev/null 2>&1
    printf "%s %s\n" "$sha" "$subject" >> "$LOG_FILE"

    p4 reconcile -ead "$DEPOT_PATH" 2>&1 | tail -3 || true

    desc=$(printf "%s\n\ngit-sha:    %s\ngit-author: %s\ngit-date:   %s" \
        "$subject" "$sha" "$author" "$cdate")
    if [ -n "$body" ]; then
        desc=$(printf "%s\n\n%s" "$desc" "$body")
    fi

    if ! p4 submit -d "$desc" 2>&1 | tail -3; then
        echo "$PROGRESS_PREFIX submit failed for $sha — leaving open files for inspection" >&2
        FAILED=1
        break
    fi
done

if [ "$FAILED" -eq 1 ]; then
    exit 1
fi

echo "$PROGRESS_PREFIX done — $TOTAL CL(s) submitted"
