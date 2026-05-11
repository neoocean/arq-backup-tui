# Arq.app destination fixtures

This directory holds **operator-supplied** small Arq.app v8
destination captures used for automated Strategy B / C / I-alt
regression testing.

## Why fixtures live outside the repo

Each captured destination contains:

- Real (small) source content the operator chose to expose
- ARQO-encrypted blobs whose encryption key is the operator's
  destination password
- Tree v4 metadata reflecting the operator's filesystem state at
  the time of capture

None of this is sensitive in the abstract — destinations are
designed to be ARQO-encrypted at rest — but to keep the repo
public-safe we don't commit any real captures. Instead, each
fixture sub-directory in this folder is **git-ignored**; the
test suite skips Strategy B / C / I-alt regression checks when
the fixtures are absent and runs them when they're present.

## Capturing a fixture

The operator runs the helper script (TBD — landed in a follow-up
PR) to produce a tarball of a known-small Arq.app destination:

```bash
# 1. Use Arq.app v8 to back up a small synthetic source
#    (e.g. a directory of ~10 files, ~100 KB total). Note the
#    destination UUID + folder UUID.

# 2. Tarball just that destination subtree.
tar cf tests/fixtures/arqapp_destinations/synthetic_v8.tar \
    -C /Volumes/<dest> <destination-uuid>/...

# 3. Stash the matching password somewhere the test can read.
#    Convention:
echo "<password>" > tests/fixtures/arqapp_destinations/synthetic_v8.password
chmod 0600 tests/fixtures/arqapp_destinations/synthetic_v8.password
```

The test runner discovers fixtures by tarball name (any
`*_v8.tar` in this directory pairs with a `*_v8.password` file).

## What runs against the fixtures

- **Strategy B** (`tests/test_strategy_b_fixture_regression.py`,
  TBD): cross-restore — our reader against an Arq.app emit.
- **Strategy C** (`tests/test_strategy_c_fixture_regression.py`,
  TBD): cross-restore — our writer's emit against the patched
  `arq_restore` binary. Requires
  `scripts/arq_restore_v4/arq_restore.bin.v4` to exist.
- **Strategy I-alt** (`tests/test_strategy_i_alt_fixture_regression.py`,
  TBD): patched-arq_restore vs our Python reader, byte-equal on
  every restored file.

Each test module's class-level skip condition checks for the
fixture file (and, where applicable, the patched binary). When
both exist, the tests run; when either is missing, the test
class is skipped with a clear "fixture absent" reason.

## Why not ship a synthetic fixture from our writer?

Strategy B is precisely "Arq.app's emit → our reader". A
fixture our writer produced would only test Strategy A
(fingerprint diff against itself) which is already automated
via `tests/test_fingerprint.py`. Strategy B's value comes from
having actual Arq.app v8 bytes in the input.

That said, an **operator with their own destination** can drop a
fixture here and immediately get CI coverage for cross-restore
regressions against future writer/reader changes. Without the
fixture, the schema-level coverage from `test_fingerprint.py`
+ the round-trip coverage from `test_serialization_round_trip.py`
+ the Strategy E coverage from `test_strategy_e_regression.py`
still pin every property except "real Arq.app bytes restore
through our reader".
