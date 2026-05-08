#!/bin/bash
# Install git hooks that auto-replay `main` commits to Perforce.
#
# Three triggers cover every way `main` can move forward:
#   - post-merge:    `git pull` / `git merge` integrated commits
#   - post-commit:   direct `git commit` on main (rare but possible)
#   - post-rewrite:  `git rebase` / `git commit --amend` rewrote main
#
# Each hook is a one-line shim that delegates to
# scripts/sync-main-to-p4.sh, which handles the cursor + actual
# submit logic. Re-running this installer is safe — existing
# arq-tui hooks are overwritten, hooks owned by something else
# (e.g. husky) are preserved with a `.local` suffix so the
# operator can hand-merge.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_DIR="$PROJECT_DIR/.git/hooks"
SYNC_SCRIPT="$PROJECT_DIR/scripts/sync-main-to-p4.sh"
MARKER="# arq-tui p4 sync hook (install-p4-sync-hooks.sh)"

if [ ! -x "$SYNC_SCRIPT" ]; then
    chmod +x "$SYNC_SCRIPT" 2>/dev/null || true
fi

if [ ! -d "$HOOKS_DIR" ]; then
    echo "ERROR: $HOOKS_DIR doesn't exist — is this a git repo?" >&2
    exit 1
fi

install_hook() {
    local name=$1
    local path="$HOOKS_DIR/$name"

    # If a non-arq-tui hook is already in place, preserve it.
    if [ -f "$path" ] && ! grep -q "$MARKER" "$path" 2>/dev/null; then
        mv "$path" "${path}.local"
        echo "preserved existing $name as ${name}.local"
    fi

    cat > "$path" <<EOF
#!/bin/bash
$MARKER
# Replay any new commits on main into Perforce after this git op.
# Errors are surfaced to stderr but never block the git command.
"$SYNC_SCRIPT" || true

# If the operator had a previous hook, run it after our sync so
# the original behaviour is preserved.
if [ -x "${path}.local" ]; then
    "${path}.local" "\$@" || true
fi
EOF
    chmod +x "$path"
    echo "installed $name"
}

install_hook post-merge
install_hook post-commit
install_hook post-rewrite

echo
echo "Hooks installed. Test with:"
echo "  $SYNC_SCRIPT"
echo
echo "Uninstall with:"
echo "  rm $HOOKS_DIR/post-merge $HOOKS_DIR/post-commit $HOOKS_DIR/post-rewrite"
