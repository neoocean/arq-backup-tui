#!/usr/bin/env bash
#
# build.sh — produce a Tree-v4-capable arq_restore binary on macOS.
#
# Usage:
#   ./build.sh [<arq_restore-checkout-root>] [<output-binary-path>]
#
# Defaults to:
#   src   = /private/tmp/strategy-c/arq_restore
#   out   = /private/tmp/strategy-c/arq_restore.bin.v4
#
# The script:
#   1. clones https://github.com/arqbackup/arq_restore into <src> if it's
#      not already a checkout (will leave it alone otherwise).
#   2. applies 0001-arq7-node-read-v4-trailing-block.patch (idempotent —
#      skips with a notice if the file already contains the patch).
#   3. compiles the project with clang + OpenSSL path, linking against
#      the prebuilt 3rd-party .o files we ship alongside.
#
# Prerequisites: Xcode CLT + clang (no full Xcode required). The
# vendored 3rdparty/openssl-1.1.1h gives static libcrypto/libssl;
# the rest is system frameworks.
#
# Why we ship this:
#   `arq_restore`'s upstream `Arq7Node::initWithBufferedInputStream:`
#   has no `theTreeVersion >= 4` branch and stops 38 bytes short of
#   where the next Node begins. That makes Tree v4 destinations
#   (which Arq.app 7.40+ emits) unrestorable through arq_restore.
#   This patch adds the missing 38-byte read, making arq_restore a
#   working Tree v4 reference reader — i.e. an Arq.app-GUI-free way
#   to byte-verify our writer's v4 emit and to cross-check our
#   reader against an independent implementation.
#
#   See docs/COMPAT-VERIFICATION.md §5.8 for the verification
#   workflow this script enables.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_FILE="$SCRIPT_DIR/0001-arq7-node-read-v4-trailing-block.patch"

ARQR_SRC="${1:-/private/tmp/strategy-c/arq_restore}"
ARQR_BIN="${2:-/private/tmp/strategy-c/arq_restore.bin.v4}"

# 1. Source checkout — clone if absent.
if [ ! -d "$ARQR_SRC/.git" ] && [ ! -f "$ARQR_SRC/arq_restore.xcodeproj/project.pbxproj" ]; then
    echo "==> Cloning arqbackup/arq_restore into $ARQR_SRC"
    git clone https://github.com/arqbackup/arq_restore "$ARQR_SRC"
fi

# 2. Apply v4 patch if it isn't already present (idempotent).
ARQ7NODE="$ARQR_SRC/arq7restore/Arq7Node.m"
if grep -q "theTreeVersion >= 4" "$ARQ7NODE"; then
    echo "==> v4 patch already present in $ARQ7NODE — skipping"
else
    echo "==> Applying $PATCH_FILE to $ARQR_SRC"
    (cd "$ARQR_SRC" && patch -p1) <"$PATCH_FILE"
fi

# 3. Build. Strategy:
#    - 5 SBJSON files + lz4 + sqlite3 are compiled with -fno-objc-arc
#      (per the upstream xcodeproj's per-file COMPILER_FLAGS). We
#      pre-build these into <strategy-c>/obj/ and reuse them.
#    - Everything else compiles with -fobjc-arc + the prefix header.
PREBUILT_OBJ_DIR="$(dirname "$ARQR_SRC")/obj"
PREBUILT_OBJS=(
    "$PREBUILT_OBJ_DIR/SBJsonBase.o"
    "$PREBUILT_OBJ_DIR/SBJsonWriter.o"
    "$PREBUILT_OBJ_DIR/SBJsonParser.o"
    "$PREBUILT_OBJ_DIR/NSObject+SBJSON.o"
    "$PREBUILT_OBJ_DIR/NSString+SBJSON.o"
    "$PREBUILT_OBJ_DIR/lz4.o"
    "$PREBUILT_OBJ_DIR/sqlite3.o"
)

# Pre-build the no-arc .o files on demand.
mkdir -p "$PREBUILT_OBJ_DIR"
declare -A NO_ARC_SOURCES=(
    [SBJsonBase.o]="3rdparty/json-framework/SBJsonBase.m"
    [SBJsonWriter.o]="3rdparty/json-framework/SBJsonWriter.m"
    [SBJsonParser.o]="3rdparty/json-framework/SBJsonParser.m"
    [NSObject+SBJSON.o]="3rdparty/json-framework/NSObject+SBJSON.m"
    [NSString+SBJSON.o]="3rdparty/json-framework/NSString+SBJSON.m"
    [lz4.o]="3rdparty/lz4/lz4.c"
    [sqlite3.o]="3rdparty/sqlite-amalgamation-3340000/sqlite3.c"
)
for o in "${!NO_ARC_SOURCES[@]}"; do
    src="${NO_ARC_SOURCES[$o]}"
    obj="$PREBUILT_OBJ_DIR/$o"
    abs_src="$ARQR_SRC/$src"
    if [ ! -f "$abs_src" ]; then
        # Try common alternative locations
        for alt in 3rdparty/sbjson 3rdparty/SBJson 3rdparty 3rdparty/lz4-1.9.4 3rdparty/lz4-r131; do
            cand="$ARQR_SRC/$alt/$(basename "$src")"
            if [ -f "$cand" ]; then abs_src="$cand"; break; fi
        done
    fi
    if [ ! -f "$obj" ] || [ "$abs_src" -nt "$obj" ]; then
        if [ -f "$abs_src" ]; then
            echo "==> Compiling $abs_src -> $obj"
            clang -c -fno-objc-arc -Wno-everything -I"$ARQR_SRC" -I"$(dirname "$abs_src")" \
                -include "$ARQR_SRC/arq_restore_Prefix.pch" \
                -DUSE_OPENSSL=1 \
                -o "$obj" "$abs_src"
        else
            echo "warn: pre-built object $o requested but source not found; skipping"
        fi
    fi
done

# Build the include flag list.
INCDIRS=()
while IFS= read -r d; do
    INCDIRS+=("-I$ARQR_SRC/${d#./}")
done < "$ARQR_SRC/../incdirs.txt"

# Every .m except the no-arc ones.
M_FILES=()
while IFS= read -r f; do
    M_FILES+=("$f")
done < <(find "$ARQR_SRC" -name "*.m" | \
        grep -vE "(SBJsonBase|SBJsonWriter|SBJsonParser|NSObject\+SBJSON|NSString\+SBJSON|lz4|sqlite3)\.m$")

echo "==> Compiling + linking $ARQR_BIN ($(wc -l <<<"$(printf '%s\n' "${M_FILES[@]}")") .m files)"
clang -fobjc-arc -DUSE_OPENSSL=1 \
    -include "$ARQR_SRC/arq_restore_Prefix.pch" \
    -Wno-everything \
    "${INCDIRS[@]}" \
    -L"$ARQR_SRC/3rdparty/openssl-1.1.1h/lib" \
    -lcrypto -lssl -lz \
    -framework Foundation -framework SystemConfiguration -framework Security \
    -framework CoreFoundation -framework CoreServices -framework IOKit \
    -framework AppKit \
    -o "$ARQR_BIN" \
    "${M_FILES[@]}" \
    "${PREBUILT_OBJS[@]}"

echo "==> Built $ARQR_BIN"
"$ARQR_BIN" 2>&1 | head -3 || true
