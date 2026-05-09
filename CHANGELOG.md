# Changelog

Auto-generated from the git log by `scripts/build_changelog.py`. Each entry shows the commit subject + (PR if available) + short SHA. Re-run the script to regenerate after merging more PRs.

## 2026-05

### Features

- Group 4: restore conflict resolution (E6) + backup safety scenario tests (G2) [e358159](https://github.com/neoocean/arq-backup-tui/commit/e358159)
- Group 3: multi-dest cancel + subprocess pause/resume precision [309707f](https://github.com/neoocean/arq-backup-tui/commit/309707f)
- Group 2: --debug logging (F4) + .secrets/ wizard helper (F1) [3102808](https://github.com/neoocean/arq-backup-tui/commit/3102808)
- Feature completion bundle: 14 contained additions across A/E/D/G/B/C series [7fa9260](https://github.com/neoocean/arq-backup-tui/commit/7fa9260)
- Add Arq 7 compatibility verification tooling (operator-paste workflows) [75abc49](https://github.com/neoocean/arq-backup-tui/commit/75abc49)
- Add Arq 7 format-conformance verification [4cf407d](https://github.com/neoocean/arq-backup-tui/commit/4cf407d)
- Add tree-walk reuse for cross-run dedup [18a9f1b](https://github.com/neoocean/arq-backup-tui/commit/18a9f1b)
- Add cross-run dedup for the writer [09a1f36](https://github.com/neoocean/arq-backup-tui/commit/09a1f36)
- Add docs/COVERAGE.md: feature matrix vs. Arq 5 / 6 / 7 [5e1030d](https://github.com/neoocean/arq-backup-tui/commit/5e1030d)
- Add Makefile + GitHub Actions test workflow [acf0f5f](https://github.com/neoocean/arq-backup-tui/commit/acf0f5f)
- Add (min,max) chunker-constant pair-search heuristic [f08109e](https://github.com/neoocean/arq-backup-tui/commit/f08109e)
- Add chunker-parameter falsification harness [6a2cf38](https://github.com/neoocean/arq-backup-tui/commit/6a2cf38)
- Add Arq 5/6 restorer + Buzhash content-defined chunker [151bd9a](https://github.com/neoocean/arq-backup-tui/commit/151bd9a)
- Add arq_reader: writer round-trip restore [0fba23a](https://github.com/neoocean/arq-backup-tui/commit/0fba23a)
- Add arq_writer: Arq 7 backup creator [d19f238](https://github.com/neoocean/arq-backup-tui/commit/d19f238)
- Add SFTP backend and resumable audit-drip [ab4097a](https://github.com/neoocean/arq-backup-tui/commit/ab4097a)
- Add independent Arq 7 backup validator [5d81e52](https://github.com/neoocean/arq-backup-tui/commit/5d81e52)

### Fixes

- Fix CI: skip race-during-walk test on Python 3.9 (pathlib differs) [55292f9](https://github.com/neoocean/arq-backup-tui/commit/55292f9)
- Fix CI: SchedulingStagedPlan tests need a running App on Python 3.9 [96f065b](https://github.com/neoocean/arq-backup-tui/commit/96f065b)
- Fix all 14 stale doc references found by D1's checker + wire it into CI [a352920](https://github.com/neoocean/arq-backup-tui/commit/a352920)
- Fix CI: thread apfs_snapshot fallback through new _run_backup_call [72f7f06](https://github.com/neoocean/arq-backup-tui/commit/72f7f06)
- Fix CI: migrate every plistlib.loads(record) site to dual-format helper [b4315cf](https://github.com/neoocean/arq-backup-tui/commit/b4315cf)

### Docs

- Translate all GitHub-visible documentation to English [204e606](https://github.com/neoocean/arq-backup-tui/commit/204e606)
- Sync DESIGN.md + COVERAGE.md with new modules (runs/runs_monitor/machine_info/console) [f43d45b](https://github.com/neoocean/arq-backup-tui/commit/f43d45b)
- README: state intent, scope, and Arq Backup attribution [d72533c](https://github.com/neoocean/arq-backup-tui/commit/d72533c)
- Document real-SFTP-data discoveries before/after [53bc0c7](https://github.com/neoocean/arq-backup-tui/commit/53bc0c7)
- Sync all docs with current implementation (PRs #5–#12) [81edb8a](https://github.com/neoocean/arq-backup-tui/commit/81edb8a)
- Document backup / validate / restore mechanism in Korean [4c862a5](https://github.com/neoocean/arq-backup-tui/commit/4c862a5)
- Plan the TUI implementation [59cb1ed](https://github.com/neoocean/arq-backup-tui/commit/59cb1ed)
- Lock storage-backend scope to local + NAS + SFTP [a708a78](https://github.com/neoocean/arq-backup-tui/commit/a708a78)
- Re-audit COVERAGE.md against the full Arq 7 feature surface [aba4a80](https://github.com/neoocean/arq-backup-tui/commit/aba4a80)
- Restrict COVERAGE.md to Arq 7 only [be1de25](https://github.com/neoocean/arq-backup-tui/commit/be1de25)
- Tighten backup-creation feasibility research notes [54a75a0](https://github.com/neoocean/arq-backup-tui/commit/54a75a0)
- Document project design and backup-creation feasibility [86d9c4e](https://github.com/neoocean/arq-backup-tui/commit/86d9c4e)

### Tests

- Deflake macho-finder tests by seeding the planted T table [8ab01b0](https://github.com/neoocean/arq-backup-tui/commit/8ab01b0)
- Make tests discoverable via 'unittest discover -s tests' [e2760dd](https://github.com/neoocean/arq-backup-tui/commit/e2760dd)

### Internal

- Wire up four completed-but-unreachable modules (G1-b + A4-b + B9-c + H2) [d2e7132](https://github.com/neoocean/arq-backup-tui/commit/d2e7132)
- Loosen pyright config: demote pre-existing dict-spread / dynamic-dispatch errors to warnings [cb3e8da](https://github.com/neoocean/arq-backup-tui/commit/cb3e8da)
- B8+B9+D1+D2+D3+D4: TUI sidebar, uid/gid restore, doc link checker, coverage, API docs, nightly CI [e306f48](https://github.com/neoocean/arq-backup-tui/commit/e306f48)
- B2+B3+B4+B7: pause/resume + multi-destination + snapshot diff + multi-computer [b072815](https://github.com/neoocean/arq-backup-tui/commit/b072815)
- C1+C2+C3+B5: scale-confirm Tree v4 block, switch xattrs to XAttrSetV002, multi-source/SFTP CLI, scheduling [ead057d](https://github.com/neoocean/arq-backup-tui/commit/ead057d)
- Auto-reconnect SFTP ControlMaster on idle drop [c84d042](https://github.com/neoocean/arq-backup-tui/commit/c84d042)
- Real-data integration + Tree v4 wire-up + plan-edit + restore ETA + throttle measurement [9da99e2](https://github.com/neoocean/arq-backup-tui/commit/9da99e2)
- macOS fidelity batch: xattrs + record validation + hardlinks + SFTP backoff + Tree v4 emit [3fe6a9d](https://github.com/neoocean/arq-backup-tui/commit/3fe6a9d)
- Dual-mode BackupRunScreen + Arq 7 pack walker + Tree v4 trailing-block RE [e0a9b0f](https://github.com/neoocean/arq-backup-tui/commit/e0a9b0f)
- Resolve circular import + cover remaining plist callers + prior_tree owner-name reuse [d240c0a](https://github.com/neoocean/arq-backup-tui/commit/d240c0a)
- Source-machine identification + deeper compat tests + doc sync [2b98596](https://github.com/neoocean/arq-backup-tui/commit/2b98596)
- CLI / TUI process split: state-file IPC + Activity monitor [4c55b7c](https://github.com/neoocean/arq-backup-tui/commit/4c55b7c)
- Tree v4 binary format support (38-byte per-node trailing block) [60496a1](https://github.com/neoocean/arq-backup-tui/commit/60496a1)
- list_backuprecords: drop incorrect bucket-formula claim [7ded492](https://github.com/neoocean/arq-backup-tui/commit/7ded492)
- Arq.app real-world format compatibility (4 fixes + new tests) [399480a](https://github.com/neoocean/arq-backup-tui/commit/399480a)
- SftpBackend: chrooted SFTP-only compat (Hetzner Storage Box etc.) [792e521](https://github.com/neoocean/arq-backup-tui/commit/792e521)
- Auto-replay main commits to Perforce via git hooks [7475bbc](https://github.com/neoocean/arq-backup-tui/commit/7475bbc)
- Real-destination reader/validator/writer integration suite via .secrets/ [661fbb9](https://github.com/neoocean/arq-backup-tui/commit/661fbb9)
- Quake-style command console (slash-commands, slides up from bottom) [000ec09](https://github.com/neoocean/arq-backup-tui/commit/000ec09)
- Batch A: Restore ETA + Plan editing UI + Hetzner rate-limit detection [95295b4](https://github.com/neoocean/arq-backup-tui/commit/95295b4)
- TUI integration of post-M6 features: exclusions, APFS, retention, password rotation [7a393ca](https://github.com/neoocean/arq-backup-tui/commit/7a393ca)
- Retention policy, record pruning, and conservative blob GC [60d753c](https://github.com/neoocean/arq-backup-tui/commit/60d753c)
- Expose new writer features through arq-backup CLI [bf36b1f](https://github.com/neoocean/arq-backup-tui/commit/bf36b1f)
- Real-SFTP integration test harness for Arq 7 compat verification [9461261](https://github.com/neoocean/arq-backup-tui/commit/9461261)
- APFS snapshot-based backup option for macOS [2dee456](https://github.com/neoocean/arq-backup-tui/commit/2dee456)
- Unicode + multi-language + emoji + long-path stress suite [d488260](https://github.com/neoocean/arq-backup-tui/commit/d488260)
- GUI parity: metadata + symlinks + filters + largeblobpacks + plan CLI + chunker override + password rotation [dfa160d](https://github.com/neoocean/arq-backup-tui/commit/dfa160d)
- M6: polish (help screen, theme toggle, keyring extra) [4c74081](https://github.com/neoocean/arq-backup-tui/commit/4c74081)
- M5: validation runner (4 tiers + audit-drip) [b2daa10](https://github.com/neoocean/arq-backup-tui/commit/b2daa10)
- M4: restore execution screen + selective restore [88e96df](https://github.com/neoocean/arq-backup-tui/commit/88e96df)
- M3: plan wizard + backup execution + worker bridge [151c6db](https://github.com/neoocean/arq-backup-tui/commit/151c6db)
- M2: backup-set + record browser screens [5a0ce02](https://github.com/neoocean/arq-backup-tui/commit/5a0ce02)
- M1: arq_tui skeleton (App + Home + theming + smoke tests) [7f99110](https://github.com/neoocean/arq-backup-tui/commit/7f99110)
- Precursor: 4 library APIs the TUI consumes [0be6684](https://github.com/neoocean/arq-backup-tui/commit/0be6684)
- Achieve the SFTP storage-backend goal across reader and writer [440bcd1](https://github.com/neoocean/arq-backup-tui/commit/440bcd1)
- Walk every folder's tree for multi-folder cross-run dedup [c09c8a3](https://github.com/neoocean/arq-backup-tui/commit/c09c8a3)
- Land Arq.app v7 chunker parameters from Mach-O RE [613b419](https://github.com/neoocean/arq-backup-tui/commit/613b419)
- RE toolkit for Arq.app's exact chunker parameters [958eb94](https://github.com/neoocean/arq-backup-tui/commit/958eb94)
- Build Arq 7 PackBuilder + Arq 5/6 binary parsers [9222ee6](https://github.com/neoocean/arq-backup-tui/commit/9222ee6)
- Extend reader for pack-stored blobs + Arq 5/6 .pack/.index parsers [736b5e3](https://github.com/neoocean/arq-backup-tui/commit/736b5e3)
- Initial commit [d43d793](https://github.com/neoocean/arq-backup-tui/commit/d43d793)
