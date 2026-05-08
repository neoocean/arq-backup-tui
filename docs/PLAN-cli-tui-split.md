# CLI / TUI 분리 + 외부 프로세스 모니터링 + Arq 7 GUI mimicry

## 0. 동기

현재 구조는 TUI 가 자기 자신의 worker thread 에서 직접 백업/복원/검증을
실행합니다 (`BackupWorker`, `RestoreWorker`, `ValidateWorker` 가 in-process).
이 모델은 두 가지 사용 시나리오를 막습니다:

1. **cron 등 무인 실행**. 백업이 TUI 없이 주기적으로 돌아야 하는데,
   현재 `arq-backup` CLI 는 존재하지만 진행 상황을 외부에서 관찰할
   수단이 없습니다.
2. **이미 실행 중인 백업의 상태 확인**. 운영자가 이미 한 터미널에서
   백업을 실행하고 다른 창에서 TUI 를 열어 진행률 / 현재 파일 / 실패
   목록을 보고 싶을 때, 두 프로세스가 통신할 채널이 없습니다.

또한 Arq 7 macOS GUI 의 작동 방식이 이미 그렇습니다 — `Arq.app` 자체가
하나의 프로세스로 백업을 돌리고, 사용자 GUI 는 그 프로세스의 상태를
관찰·제어합니다. 본 프로젝트는 그 모델을 유사하게 따라 갑니다.

## 1. 결과물 (shipping outcomes)

이 PR 시리즈가 끝나면 다음이 가능해집니다:

```sh
# 시나리오 A — cron 친화 (TUI 없이)
arq-backup create ~/Documents \
    --dest /Volumes/arq --password "$ARQ_PW" \
    --state-file ~/.local/state/arq-backup-tui/runs/foo.json
# → state file 에 진행률 기록, 종료 시 status=completed/failed

# 시나리오 B — 운영자가 별도 터미널에서 모니터링
./arq-tui.py
# 키 'a' 를 누르면 Activity Monitor 화면 → 시나리오 A 로 시작된
# 모든 활성 / 최근 종료 run 들이 목록으로 표시. 진행률 / 현재 파일 /
# ETA 가 1초 간격으로 갱신됨.

# 시나리오 C — TUI 안에서 새 backup 시작 (기존 동작 + spawn 모드)
./arq-tui.py → [n]ew plan → wizard → [r]un
# 기본은 subprocess spawn (CLI 와 동일 IPC), legacy in-process 도 옵션.

# 시나리오 D — 종료된 run 사후 분석
./arq-tui.py → Activity → 완료된 run 선택
# state file 의 events log + final stats + 실패 항목 surface.
```

## 2. 프로세스 모델

```
┌──────────────────┐                ┌──────────────────────────────┐
│ TUI (process A)  │                │ Backup CLI (process B)        │
│ ── monitor only  │←─ poll fs ─────│ writes state file every event │
│ display + cancel │  (1s)          │ exit code reflects outcome    │
└──────────────────┘                └──────────────────────────────┘
        │                                       │
        ↓                                       ↓
        ┌──────────────────────────────────────────────┐
        │ $XDG_STATE_HOME/arq-backup-tui/runs/<id>.json │
        │ (atomic write: write tmp + rename)            │
        └──────────────────────────────────────────────┘
```

핵심 원칙:
- **단방향 통신만**: CLI 는 state file 만 쓴다, TUI 는 읽기만. 양방향
  IPC (lock 등) 없이도 race condition 이 깨끗하게 회피됩니다.
- **atomic write**: state file 변경은 모두 `write tmp + rename`. 부분 read
  발생하지 않음.
- **Cancellation**: TUI 가 cancel 을 요청하려면 CLI 의 PID 에 `SIGTERM`
  를 보낸다. CLI 는 graceful cancel 처리 (이미 `Backup.cancel` 메커니즘
  존재).
- **Liveness check**: TUI 는 state file 의 PID 가 살아있는지 `kill(pid,
  0)` 로 확인. 죽었지만 status=running 인 경우 → "stale" 로 표시.

## 3. State file 포맷

```jsonc
// ~/.local/state/arq-backup-tui/runs/<run-id>.json
{
  "schema_version": 1,
  "run_id": "01J…UUID",                 // ULID 또는 UUIDv7
  "kind": "backup|restore|validate",
  "status": "starting|running|completed|failed|cancelled|stale",
  "started_at": 1771678551,             // unix epoch sec
  "finished_at": null,                  // null while running
  "pid": 12345,
  "host": "home-laptop.local",          // for multi-host destinations
  "plan_id": "UUID-or-null",            // backup mode only
  "plan_name": "home-laptop-to-nas",
  "destination": {
    "kind": "local|sftp",
    "label": "/Volumes/arq",
    "computer_uuid": "..."
  },
  "progress": {
    "files_total": null,                // null when planning incomplete
    "files_done": 1234,
    "bytes_total": null,
    "bytes_done": 5678901,
    "current_path": "/Users/.../foo.txt",
    "throughput_bps": 4_512_000,
    "eta_sec": 1247
  },
  "result": null,                       // populated on terminal status
  "events_tail": [                      // last N events (ring buffer)
    {"t": 1771678555, "kind": "file_written", "path": "...", "size": 1024},
    ...
  ],
  "error": null                         // string when status=failed
}
```

업데이트 정책:
- 매 `ProgressCb` 이벤트마다 progress + events_tail (last 50) 갱신
- I/O 폭주 방지: 매 1초 또는 매 100 이벤트 중 먼저 trigger 되는 쪽 (whichever first)
- 종료 시 status + result + finished_at 즉시 flush

## 4. CLI 진입점 변경

| CLI | 변경 내용 |
|---|---|
| `arq-backup create` | 새 `--state-file <path>` 옵션. 비어 있으면 기본값 `$XDG_STATE_HOME/arq-backup-tui/runs/<auto-uuid>.json`. CLI 시작 시 즉시 status=starting 기록, `ProgressCb` 마다 progress 갱신, 종료 시 status=completed/failed |
| `arq-reader restore` | 동일 |
| `arq-validator` | 동일 |
| (신규) `arq-tui-runs ls` | 활성/최근 run 목록 (CLI 진입점) |
| (신규) `arq-tui-runs show <run-id>` | 단일 run 상세 |
| (신규) `arq-tui-runs cancel <run-id>` | PID 에 SIGTERM |
| (신규) `arq-tui-runs gc` | 30일+ 된 종료된 state file 정리 |

## 5. TUI 변경

### 5.1 신규 화면: `RunsMonitorScreen`

```
┌─ Activity ─────────────────────────────────────────────────────┐
│ Active                                                          │
│   ▶ home-laptop-to-nas    [████░░░░░░] 41%  3:21 ETA  [c]ancel  │
│   ▶ docs-to-sftp          [██░░░░░░░░] 18%  9:12 ETA  [c]ancel  │
│ Recent (last 24h)                                               │
│   ✓ home-laptop-to-nas    completed 02:14 → 03:08 (54m)         │
│   ✗ pictures-to-nas       failed    01:30 → 01:31 (network)     │
│                                                                  │
│ [n]ew run  [r]efresh  [g]c old  [Enter] details  [Esc] back     │
└──────────────────────────────────────────────────────────────────┘
```

- Polling 1Hz; state file 변경 감지는 mtime 비교
- Active 와 Recent 를 분리해서 표시
- `Enter` 로 single-run 상세 → events_tail + 실패 항목

### 5.2 기존 BackupRunScreen / RestoreRunScreen / ValidateRunScreen

Dual-mode 로 변환:
- **Default (spawn)**: subprocess.Popen 으로 CLI 실행, state file watch
- **Legacy (`--in-process`)**: 기존 worker thread 방식 (테스트/디버그용)

이렇게 하면 새 spawn 모드가 cron 시나리오와 100% 같은 코드 경로를
탑니다 — TUI 안에서 시작하든 cron 으로 시작하든 동일한 ipc.

### 5.3 Sidebar 추가 (Arq 7 GUI 모방)

```
┌────────┬─────────────────────────────────────────────────┐
│Backup  │                                                  │
│Sets    │  (current screen)                                │
│        │                                                  │
│Activity│                                                  │
│        │                                                  │
│Plans   │                                                  │
│        │                                                  │
│Settings│                                                  │
└────────┴─────────────────────────────────────────────────┘
```

- 좌측 sidebar (10-12 cols 폭) 에 4개 섹션
- Arq 7 의 시각적 무게감을 위해 active section 은 강조 색
- 키보드 단축키 (`1`-`4`) 로 sidebar section 전환
- `t` 로 토글 (sidebar 숨기기 / 보이기)

### 5.4 색상·간격 (Arq 7 GUI 유사화)

| 요소 | Arq 7 룩 | 본 TUI 매핑 |
|---|---|---|
| Sidebar 배경 | 진한 회색 | `$panel-darken-1` |
| Active row | 옅은 파란색 | `$accent` |
| Progress bar | 푸른 그라데이션 | Textual `ProgressBar` 색상 변경 |
| Status icons | ✓ ✗ ⏸ ⏵ | 동일 unicode + 색상 |
| 폰트 weight | sidebar bold, content 일반 | `text-style: bold` 사용 |

## 6. 마이그레이션 단계 (PR 단위)

| Phase | 결과물 | 의존 |
|---|---|---|
| **P0 (이 PR)** | 본 설계 문서, `arq_tui/runs.py` 모듈 (state file IO + 단위 테스트) | - |
| **P1** | CLI 들이 `--state-file` 지원, `RunsMonitorScreen` 추가 (read-only) | P0 |
| **P2** | 기존 BackupRunScreen 등이 spawn 모드로 동작 (legacy in-process 유지) | P1 |
| **P3** | Sidebar + Arq 7 색상 팔레트 적용 | P1, P2 |
| **P4** | `arq-tui-runs` CLI (ls/show/cancel/gc) | P0 |
| **P5** | (선택) cron 친화 헬퍼: `arq-tui-cron` 명령으로 plan 등록 + 시스템 cron entry 생성 | P1 |

## 7. 호환성·안전 고려사항

- **State file 위치 표준**: `$XDG_STATE_HOME/arq-backup-tui/runs/`. macOS
  는 XDG 비표준이지만 `~/.local/state/arq-backup-tui/` 으로 fallback.
- **기존 `--in-process` 모드 유지**: 테스트 스위트가 사용 중이며, P3
  까지는 기본 옵션으로 보존. P3 이후 spawn 을 default 로.
- **stale state file**: PID 가 죽었지만 status=running 인 file 은 TUI
  에서 "stale" 로 표시 + 운영자가 수동 cleanup 가능. 자동 cleanup
  (`gc`) 은 30일 + 종료된 것만.
- **PII 보호**: state file 의 `current_path` 는 운영자의 실 파일 경로
  를 포함. 권장 chmod 0600. 그리고 events_tail 은 50개로 캡 — 무한대
  로 자라지 않음.
- **Multi-host**: 한 destination 에 여러 머신이 백업 중이면 state file
  의 `host` 필드로 구분. State files 는 host-local (각 머신마다 자기
  XDG_STATE_HOME) 이라 충돌 없음.

## 8. 비고

- TUI 의 **모든 in-screen worker** (workers.py) 는 P2 이후 spawn-only
  로 갈 것. legacy 모드는 환경변수 `ARQ_TUI_IN_PROCESS=1` 같이 escape
  hatch 로 유지.
- `arq_tui/console_commands.py` 의 `:run <plan>` 도 spawn 모드 사용.
- 기존 통합 테스트 (`test_arq_real_destination.py` 등) 의 in-process
  runner 는 그대로 유지 (CLI subprocess + state file polling 으로 옮기면
  CI 환경에서 조립이 복잡해짐).
