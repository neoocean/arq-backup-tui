# Arq compatibility matrix (accumulating)

Each row is one run of `scripts/arq_compat/run.py` against an installed Arq.app version. Re-run after every Arq update. `PASS*` = content byte-identical but a filename came back NFC/NFD-normalised (Arq restore-side behaviour, not a data gap). Per-scenario detail is in the linked report.

| Date | Arq version | Dir A (writer→Arq) | Dir B (Arq→reader) | Format drift | Report |
|---|---|---|---|---|---|
| 2026-05-25 | 7.44.1 | PASS | PASS | no prior baseline (first version captured) | [report](runs/7.44.1_2026-05-25.md) |
