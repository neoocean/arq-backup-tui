"""Strict-mode byte-equivalence checks.

The default :func:`arq_validator.compatibility.check_arq7_compatibility`
exercises *schema-level* invariants — every key Arq 7 mandates is
present with the right type and value range. Strict mode goes one
layer deeper: for every parseable on-disk artefact (backuprecord
JSON, tree binary, xattr blob), the validator does a
``parse → write → compare`` round-trip and asserts the second
emit is **byte-identical** to the first.

This is the same property ``docs/COMPAT-VERIFICATION.md`` §5.6
(Strategy F + R4) verified against the operator's real Arq.app v8
destination — Strategy F nailed down the writer's serialise layer
returns the source bytes when given a parsed Arq.app v8 blob. The
schema checker can't catch a regression in that layer (a refactor
that drops compact JSON separators, or sorts xattr dict keys
alphabetically, or stops preserving Tree v4's trailing block raw
bytes); strict mode does.

Three round-trips:

- **RT1** — BackupRecord ``parse → serialize``. Detects the
  source format (binary plist or UTF-8 JSON, the two Arq 7 emit
  shapes) and re-serialises in the same format. Drift = a
  serialise-layer regression.
- **RT2** — Tree binary ``parse → write``. Catches loss of the
  Tree v4 trailing block, drift in BlobLoc field ordering, or
  any other binary-layout regression.
- **RT3** — Xattr blob ``deserialize → serialize``. Catches loss
  of the XAttrSetV002 magic, the listxattr-order convention, or
  any of the other small invariants the format-handler enforces.

Strict mode is **opt-in** because it's substantially more
expensive than the layout sweep: every standardobject is decrypted
and parsed, not just sampled. The default ``sample_cap`` keeps
runtime bounded for huge destinations; pass ``sample_cap=None`` to
walk everything.

Failures land in the standard :class:`ComplianceReport` as
:class:`CheckResult` entries with IDs ``RT1`` / ``RT2`` / ``RT3``
so a caller can filter for them.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Tuple

from . import constants as C
from .backend import Backend
from .compatibility import CheckResult, _fail, _ok


def run_strict_round_trips(
    backend: Backend, root: str, cu: str, keyset, folder_uuids: List[str],
    *,
    sample_cap: Optional[int] = 64,
    openssl_path: str = "openssl",
) -> List[CheckResult]:
    """Run RT1–RT3 across the destination's parseable artefacts.

    Returns a list of :class:`CheckResult`. The function never
    raises for format failures — every problem lands in a result
    with ``passed=False``.

    ``sample_cap`` bounds the number of standardobjects/treepacks
    inspected per type. ``None`` walks every blob (can be expensive
    on the operator's real destinations — 415k+ standardobjects).
    """
    out: List[CheckResult] = []
    out.extend(_rt1_backuprecords(
        backend, root, cu, keyset, folder_uuids,
        openssl_path=openssl_path,
    ))
    out.extend(_rt2_and_rt3_standardobjects(
        backend, root, cu, keyset,
        sample_cap=sample_cap, openssl_path=openssl_path,
    ))
    return out


# ---------------------------------------------------------------------------
# RT1 — BackupRecord parse → serialize byte equivalence
# ---------------------------------------------------------------------------


def _rt1_backuprecords(
    backend: Backend, root: str, cu: str, keyset, folder_uuids: List[str],
    *, openssl_path: str = "openssl",
) -> List[CheckResult]:
    """For every backuprecord under ``cu/backupfolders/*/backuprecords/``:

      1. ARQO-decrypt to plaintext.
      2. ``parse_backuprecord`` → dict.
      3. ``serialize_backuprecord(dict, fmt=same-as-source)`` → bytes.
      4. Compare against the original plaintext.

    Drift = a serialise-layer regression. The fmt detection
    follows the same plist-first-then-JSON cascade
    ``parse_backuprecord`` uses internally.
    """
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_writer.backuprecord import (
        parse_backuprecord, serialize_backuprecord,
    )

    results: List[CheckResult] = []
    walked = 0
    drift = 0
    for fu in folder_uuids:
        rec_root = (
            f"{root.rstrip('/')}/{cu}/{C.BACKUPFOLDERS_DIR}/{fu}/"
            f"{C.BACKUPRECORDS_DIR}"
        )
        try:
            buckets = backend.list_dir(rec_root)
        except Exception:
            continue
        for bucket in buckets:
            if not (bucket.isdigit() and len(bucket) == 5):
                continue
            try:
                inner = backend.list_dir(f"{rec_root}/{bucket}")
            except Exception:
                continue
            for name in inner:
                if not name.endswith(".backuprecord"):
                    continue
                rec_path = f"{rec_root}/{bucket}/{name}"
                try:
                    arqo = backend.read_all(rec_path)
                    plain = decrypt_lz4_arqo(
                        arqo, keyset.encryption_key, keyset.hmac_key,
                        openssl_path=openssl_path,
                    )
                    record = parse_backuprecord(plain)
                except Exception as exc:
                    results.append(_fail(
                        "RT1", f"backuprecord decrypt + parse: {rec_path}",
                        f"{type(exc).__name__}: {exc}",
                    ))
                    continue
                fmt = _detect_record_fmt(plain)
                try:
                    re_emit = serialize_backuprecord(record, fmt=fmt)
                except Exception as exc:
                    results.append(_fail(
                        "RT1", f"backuprecord re-serialize: {rec_path}",
                        f"{type(exc).__name__}: {exc}",
                        fmt=fmt,
                    ))
                    continue
                walked += 1
                if re_emit == plain:
                    continue
                drift += 1
                results.append(_fail(
                    "RT1", f"backuprecord round-trip: {rec_path}",
                    f"re-serialize differs from source "
                    f"(orig {len(plain)} B, re-emit {len(re_emit)} B, "
                    f"first diff at byte "
                    f"{_first_diff_index(plain, re_emit)})",
                    fmt=fmt,
                ))
    if walked:
        results.append(_ok(
            "RT1", "backuprecord parse→serialize byte equivalence",
            walked=walked, drift=drift,
        ))
    return results


def _detect_record_fmt(plain: bytes) -> str:
    """Return the fmt arg for ``serialize_backuprecord`` matching
    the source bytes. Binary plist starts with ``bplist00``; JSON
    starts with ``{`` after optional whitespace."""
    if plain.startswith(b"bplist00"):
        return "plist"
    return "json"


# ---------------------------------------------------------------------------
# RT2 + RT3 — standardobjects: tree binary OR xattr blob round-trip
# ---------------------------------------------------------------------------


def _rt2_and_rt3_standardobjects(
    backend: Backend, root: str, cu: str, keyset,
    *,
    sample_cap: Optional[int] = 64,
    openssl_path: str = "openssl",
) -> List[CheckResult]:
    """Sample standardobjects, attempt to parse each as a Tree or
    xattr blob, and round-trip-byte-compare on success.

    Strategy F-3 + Strategy K-static both walk the same kind of
    sample; the difference here is that strict mode runs them
    against the operator's installed destination, not synthetic
    inputs. Both checks gracefully skip non-matching blobs (a data
    blob isn't a tree and isn't an xattr; that's not a failure,
    it's just a different artefact type)."""
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree
    from arq_writer.serialize import write_tree
    from arq_writer.xattrs import deserialize_xattrs, serialize_xattrs

    results: List[CheckResult] = []
    so_root = f"{root.rstrip('/')}/{cu}/{C.STANDARDOBJECTS_DIR}"
    if not backend.is_dir(so_root):
        # Packed-only destination; nothing to walk here. Treepacks
        # / blobpacks could be added in a future RT extension.
        results.append(_ok(
            "RT2", "standardobjects/ absent (packed-only mode)",
        ))
        return results

    try:
        shards = backend.list_dir(so_root)
    except Exception as exc:
        results.append(_fail(
            "RT2", "standardobjects/ listable",
            f"{type(exc).__name__}: {exc}",
        ))
        return results

    trees_walked = 0
    trees_drift = 0
    xattrs_walked = 0
    xattrs_drift = 0
    sampled = 0

    for shard in shards:
        if sample_cap is not None and sampled >= sample_cap:
            break
        if len(shard) != 2:
            continue
        shard_path = f"{so_root}/{shard}"
        try:
            files = backend.list_dir(shard_path)
        except Exception:
            continue
        for name in files:
            if not C.STANDARDOBJECT_NAME_RE.match(name):
                continue
            if sample_cap is not None and sampled >= sample_cap:
                break
            blob_path = f"{shard_path}/{name}"
            sampled += 1
            try:
                arqo = backend.read_all(blob_path)
                plain = decrypt_lz4_arqo(
                    arqo, keyset.encryption_key, keyset.hmac_key,
                    openssl_path=openssl_path,
                )
            except Exception:
                # Not all standardobjects are decryptable with the
                # current keyset (e.g. across keyset rotations);
                # not a strict-mode failure.
                continue

            # Try Tree first.
            tree_parsed = None
            try:
                tree_parsed = parse_tree(plain)
            except Exception:
                tree_parsed = None
            if tree_parsed is not None:
                try:
                    re_emit = write_tree(tree_parsed)
                except Exception as exc:
                    results.append(_fail(
                        "RT2", f"tree write: {blob_path}",
                        f"{type(exc).__name__}: {exc}",
                    ))
                    continue
                trees_walked += 1
                if re_emit != plain:
                    trees_drift += 1
                    results.append(_fail(
                        "RT2", f"tree round-trip: {blob_path}",
                        f"write_tree differs from source "
                        f"(orig {len(plain)} B, re-emit {len(re_emit)} B, "
                        f"first diff at byte "
                        f"{_first_diff_index(plain, re_emit)})",
                    ))
                continue

            # Not a tree — try xattr blob.
            try:
                xattrs = deserialize_xattrs(plain)
            except Exception:
                # Neither tree nor xattr — likely a data blob.
                # Not a strict-mode failure.
                continue
            if not xattrs:
                # Empty xattr blob — XAttrSetV002 with zero entries
                # is technically a tree-or-xattr ambiguity, skip.
                continue
            try:
                re_emit = serialize_xattrs(xattrs)
            except Exception as exc:
                results.append(_fail(
                    "RT3", f"xattr serialize: {blob_path}",
                    f"{type(exc).__name__}: {exc}",
                ))
                continue
            xattrs_walked += 1
            if re_emit != plain:
                xattrs_drift += 1
                results.append(_fail(
                    "RT3", f"xattr round-trip: {blob_path}",
                    f"serialize_xattrs differs from source "
                    f"(orig {len(plain)} B, re-emit {len(re_emit)} B, "
                    f"first diff at byte "
                    f"{_first_diff_index(plain, re_emit)})",
                ))

    if trees_walked:
        results.append(_ok(
            "RT2", "tree parse→write byte equivalence",
            walked=trees_walked, drift=trees_drift,
        ))
    if xattrs_walked:
        results.append(_ok(
            "RT3", "xattr deserialize→serialize byte equivalence",
            walked=xattrs_walked, drift=xattrs_drift,
        ))
    if not trees_walked and not xattrs_walked:
        results.append(_ok(
            "RT2", "no parseable tree/xattr blobs sampled",
            sampled=sampled,
        ))
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_diff_index(a: bytes, b: bytes) -> int:
    """Return the byte offset of the first differing byte between
    ``a`` and ``b``, or -1 if one is a prefix of the other (in
    which case ``min(len(a), len(b))`` is returned to signal the
    truncation point)."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))
