# PLAN — TUI 구현 계획

본 문서는 `arq-backup-tui` 프로젝트의 핵심 단계인 **TUI 프론트엔드**
구현 계획입니다. 라이브러리 (validator / reader / writer) 는 거의 완성
상태이므로, 이 문서는 그 위에 얹는 대화형 인터페이스의 설계와
단계별 구현 계획을 기술합니다.

지원 목표 기능 (사용자 요청):

1. 백업 플랜 설정 (소스 폴더, destination, 비밀번호, 청커 등)
2. 백업 실행
3. 백업 진행 상황 표시
4. 로컬 + 원격 (SFTP) 백업 세트 보기
5. 백업 세트 내부 record 조회 (트리 walk, 메타데이터)
6. 복원
7. 복원 진행 상황 표시
8. 백업 validation (L0/L1a/L1b/L2/audit-drip)

## 1. 비목표

다음 항목은 본 단계에서 다루지 않습니다 — 별도 결정/요청이 있으면
재논의합니다.

- **자동 스케줄링** (cron-like): 정책 레이어, OS-specific. TUI 는
  사용자 트리거에 의한 즉시 실행만 지원.
- **클라우드 백엔드** (S3 / B2 / Dropbox 등): COVERAGE.md 의 스코프
  결정에 따라 out of scope.
- **Mid-backup pause/resume**: pack flush 경계가 보장하는 일관성은
  유지하되 TUI 차원의 일시정지 UI는 제공하지 않음.
- **GUI 알림 / 메뉴바 통합**: TUI 만.
- **i18n 인프라**: 한국어 우선, 추후 결정.

## 2. 기술 스택 결정

### 2.1 TUI 라이브러리

후보:

| 라이브러리 | 장점 | 단점 |
|----------|------|------|
| **Textual** | reactive widget, async-native, mouse 지원, CSS-style theming, devtools, snapshot test | 의존성 추가 (rich + 그 의존성) |
| **urwid** | 성숙, mature event loop | 위젯 자체 만드는 일이 많음, 비동기 통합이 무거움 |
| **prompt-toolkit** | 폼/명령 라인 우수 | 다중 화면 레이아웃은 추가 작업 |
| **stdlib `curses`** | dep 0개 | 위젯 모두 직접 작성 — 본 스코프에 비해 비용 과다 |

**결정: Textual 채택.** 이유:

- 본 프로젝트의 모든 라이브러리 코드는 stdlib-only로 유지하고,
  Textual 의존성은 **TUI 패키지 (`arq_tui`) 안에서만** 격리합니다.
  validator / reader / writer 를 라이브러리로 임베드하는
  서드파티 사용자는 영향을 받지 않습니다.
- 진행 콜백 (`ProgressCb(kind, payload)`) 모델이 Textual 의 reactive
  속성과 1:1 로 매핑됩니다. backup/restore/validate 의 이벤트
  스트림이 reactive 위젯에 직접 와이어되어 자연스러운 라이브 업데이트.
- Textual `pilot` (headless 테스트 드라이버) 로 모든 화면을
  CI 에서 인터랙션 없이 검증 가능.
- 마우스 + 키보드 양쪽 지원, 트리/테이블/Modal 위젯 즉시 사용 가능.

### 2.2 의존성 추가 정책

`pyproject.toml`:

```toml
[project.optional-dependencies]
test = []
tui = ["textual>=0.50"]   # 신규
```

Install: `pip install -e ".[tui]"` 만 추가. CLI / 라이브러리는
**의존성 변동 없음**.

### 2.3 비밀번호 / SFTP 자격증명 저장

- 옵션 A: 매 사용 시 prompt (가장 안전, 가장 불편)
- 옵션 B: OS keyring (`keyring` 라이브러리, 옵셔널 dep)
- 옵션 C: 암호화된 config 파일

**결정: A 기본, B 선택적 활성화.** 새 의존성을 추가하지 않으려면
항상 prompt; 사용자가 `[tui-keyring]` extra 를 설치하고 설정에서
켜면 keyring 사용. 어느 경로도 평문 저장 안 함.

## 3. 패키지 구조

```
arq_tui/
├── __init__.py              # ArqTuiApp 노출
├── __main__.py              # python -m arq_tui
├── app.py                   # ArqTuiApp(textual.App) 정의
├── backend_open.py          # LocalBackend / SftpBackend open/close
├── cli.py                   # plans list/show/delete 헤드리스 서브커맨드
├── screens/
│   ├── __init__.py
│   ├── home.py              # 대시보드
│   ├── plan_wizard.py       # 새 백업 플랜 작성 흐름 (6 단계, Advanced 포함)
│   ├── backup_run.py        # 백업 실행 + 진행
│   ├── backup_sets.py       # 로컬/원격 destination 목록 + [m] maintenance 진입
│   ├── record_browser.py    # 한 record 내부 트리 walk
│   ├── restore_run.py       # 복원 실행 + 진행
│   ├── validate_run.py      # 4-tier 검증 + audit-drip
│   ├── maintenance.py       # 비밀번호 회전 + retention 적용 (PR #12)
│   └── help.py
├── widgets/
│   ├── __init__.py
│   ├── progress_panel.py    # backup/restore/validate 공용
│   ├── source_picker.py     # 소스 폴더 다중 선택
│   ├── destination_modal.py # local path / SFTP 입력 모달
│   ├── password_modal.py    # 비밀번호 prompt 모달
│   └── restore_target_modal.py
├── state.py                 # Plan / Destination / PlanRegistry / DestinationStore / CredentialCache
├── workers.py               # 스레드 작업 + ProgressCb 브리지 (BackupWorker / RestoreWorker / ValidateWorker)
└── theming.css              # 색상, 여백 등 CSS

# 추가로 repo 루트에:
arq-tui.py                   # 루트 진입점 (./arq-tui.py 로 즉시 실행 가능)
```

`arq_tui/` 는 `arq_validator` / `arq_reader` / `arq_writer` 를 import
하는 **사용자**입니다. 라이브러리 → TUI 방향의 import 는 없음.

## 4. 화면 카탈로그

각 화면은 Textual `Screen` 서브클래스. `app.push_screen` / `pop_screen`
스택으로 네비게이션.

### 4.1 Home (`home.py`)

레이아웃:

```
┌─ arq-backup-tui ──────────────────────────────────────────┐
│                                                            │
│  Plans                                                     │
│  ─────────────────────────                                 │
│  ▶ home-laptop-to-nas        last run: 2026-05-08 03:14   │
│    docs-to-sftp              never run                     │
│    + New plan                                              │
│                                                            │
│  Quick actions                                             │
│  ─────────────────────────                                 │
│    Browse backup sets   [b]                                │
│    Validate destination [v]                                │
│    Quit                 [q]                                │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

키 바인딩: `n` 새 플랜, `r` run, `b` browse, `v` validate, `q` quit.

상태: `state.PlanRegistry` 에서 plans 로드. 각 플랜의 last-run 시각은
destination 의 가장 최근 backuprecord 의 mtime 으로 결정 (프롬프트
없이 알 수 있는 메타데이터만 사용).

### 4.2 Plan wizard (`plan_wizard.py`)

`Screen` 으로 6 단계 (PR #12 에서 5 → 6 단계로 확장):

1. **Sources** — `SourcePicker` 위젯. 트리 뷰에서 다중 선택. 사용자가 선택한
   absolute path 들을 누적.
2. **Destination** — `DestinationPicker`:
   - 로컬: 디렉터리 선택기
   - SFTP: host / port / user / 인증 방식 (password / identity_file) /
     remote root path
3. **Encryption** — 비밀번호 입력 (확인 포함, masked). 세션 동안
   `CredentialCache` 에 캐시되어 mid-run prompt 가 발생하지 않음.
4. **Chunker** — 라디오:
   - "Generic Buzhash (default)" — `ChunkerConfig()` 기본값
   - "Match Arq.app v7.41" — `arq_writer.arq_chunker_params` import
   - "No chunking (single blob per file)" — `chunker_config=None`
   + Storage layout (packs vs standalone) + Cross-run dedup (on/off) 라디오.
5. **Advanced** (PR #12) — 모두 optional:
   - Exclude wildcards / regexes / .gitignore lines (각 TextArea, 한 줄당 한 패턴)
   - Skip files larger than (bytes; 빈칸 = 무제한)
   - Use APFS snapshot (macOS only; non-macOS 면 자동 fallback)
   - Retention 정책: keep_last_n / keep_daily / keep_weekly / keep_monthly / keep_yearly
6. **Review + Save** — 요약 표시, 플랜 이름 입력, 저장.

저장 위치: `~/.config/arq-backup-tui/plans/<plan-id>.json` (비밀번호
미저장; SFTP 자격증명은 §2.3 정책에 따름).

### 4.3 Backup run (`backup_run.py`)

진입: Home 에서 플랜 선택 → "Run" → 비밀번호 prompt (필요 시) →
이 화면.

레이아웃:

```
┌─ Backup: home-laptop-to-nas ──────────────────────────────┐
│ ┌─ Progress ──────────────────┐ ┌─ Stats ──────────────┐  │
│ │ ▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░ 42 % │ │ Files written:  1234 │  │
│ │                              │ │ Files reused:    987 │  │
│ │ Current file:                │ │ Bytes plain:    2 GB │  │
│ │   /home/.../foo.bin          │ │ Bytes on disk:  1 GB │  │
│ │                              │ │ Dedup ratio:   1.97x │  │
│ │ Throughput:  12 MB/s         │ │ Packs flushed:    14 │  │
│ │ ETA:         00:03:14        │ │ Trees written:   124 │  │
│ └──────────────────────────────┘ └──────────────────────┘  │
│ ┌─ Live events (last 50) ──────────────────────────────┐  │
│ │ 03:14:22 file_written /a/b.txt size=1234 chunks=1    │  │
│ │ 03:14:22 file_reused  /a/c.txt rel_path=a/c.txt      │  │
│ │ ...                                                    │  │
│ └────────────────────────────────────────────────────────┘  │
│                              [Esc] cancel    [Enter] OK     │
└────────────────────────────────────────────────────────────┘
```

내부 동작:

- `workers.run_backup(plan, password, callback)` 가 `asyncio.to_thread`
  로 `arq_writer.build_backup` 을 호출.
- 콜백은 `app.call_from_thread(self._on_event, kind, payload)` 로
  메인 루프에 이벤트 push.
- `Reactive` 카운터가 위젯에 자동 반영.
- 완료 시 BackupResult 요약을 modal 로 표시.
- 취소: `Esc` → worker 에 cancel 이벤트 → 현재 진행 중 chunk 종료 후
  pack flush + 부분 backuprecord 작성 안 함 (안전한 abort).

### 4.4 Backup set list (`backup_sets.py`)

소스: 등록된 destination 목록 (플랜에서 추출) + 최근 직접 입력
destination.

레이아웃: 좌측 destination 목록, 우측 선택된 destination 의
computer_uuid → folder_uuid → backuprecord 트리.

```
┌─ Backup sets ─────────────────────────────────────────────┐
│ Destinations           │ /Volumes/arqbackup1               │
│ ─────────────────────  │ ───────────────────────────────── │
│ ▶ /Volumes/arqbackup1  │ ▼ A714-... (laptop)               │
│   sftp:hetzner:/store  │   ▼ Folder 1: home-laptop-to-nas  │
│   + Open destination   │     ◦ 2026-05-08 03:14 [latest]   │
│                        │     ◦ 2026-05-07 03:14            │
│                        │     ◦ 2026-05-06 03:14            │
│                        │   ▶ Folder 2: docs-to-...         │
│                        │ ▶ B832-... (workstation)          │
│ [a] add  [v] validate  [m] maintenance  [Esc] back         │
└────────────────────────────────────────────────────────────┘
```

키 바인딩 `[m]` (PR #12): 현재 destination 의 캐시된 비밀번호로
`MaintenanceScreen` (§4.8) 진입.

라이브러리 사용:

- 로컬: `arq_validator.layout.discover_layout(LocalBackend(path), "/")`
- SFTP: `discover_layout(SftpBackend(host=..., root=path), "/")`

레코드 메타데이터 (creation_date, file_count 등) 는 `Restore.layouts()`
가 채우는 정보 + backuprecord 의 `creationDate` 필드로 표시.

### 4.5 Record browser (`record_browser.py`)

선택된 backuprecord 의 트리 walk. lazily 트리 blob 을 fetch 하면서
파일 시스템 트리처럼 펼치기.

레이아웃: 좌측 트리, 우측 선택된 노드의 메타데이터.

```
┌─ Record: 2026-05-08 03:14 (home-laptop-to-nas / A714-...)─┐
│ Tree                       │ Selected                       │
│ ─────────────────────────  │ ─────────────────────────────  │
│ ▼ /home/me                 │ Path: /home/me/Documents/      │
│   ▼ Documents              │       resume.pdf               │
│     ◦ resume.pdf           │ Size: 234,567 bytes            │
│     ◦ taxes/               │ mtime: 2026-04-22 11:03        │
│   ▶ Pictures               │ Mode: 0644                     │
│   ▶ Videos                 │ Blob ID: 0dde...f15f15         │
│                            │ Chunks: 3 dataBlobLocs         │
│                            │   [0] packed offset=0  len=...│
│                            │   [1] packed offset=... len=..│
│                            │   [2] packed offset=... len=..│
│ [r] mark-restore  [Esc] back  [Space] expand                │
└────────────────────────────────────────────────────────────┘
```

여러 항목을 `r` 로 마크 후 Restore 화면으로 진행.

라이브러리 사용:

- `Restore` 인스턴스 + `arq_writer.prior_tree.PriorTreeIndex` 의
  lazy walk 로직 재사용 (또는 별도 `RecordWalker` 추출).
- 트리 blob fetch 결과는 화면 종료 전까지 메모리 캐시.

### 4.6 Restore run (`restore_run.py`)

진입:

- Record browser 에서 마크된 항목 → "Restore selected" → 이 화면, 또는
- Home → Plans → "Restore latest" 단축 경로 (full-folder restore).

레이아웃: backup_run 과 동일 진행 패널 + 별도 stats:

```
Files restored:  ###      Bytes restored:    ###
Symlinks set:    ###      Errors:             ###
ETA:           ##:##      Throughput:    ## MB/s
```

내부 동작: `arq_reader.Restore.restore` + 콜백. backend 는 backup set
화면에서 사용한 backend 재사용.

### 4.7 Validate run (`validate_run.py`)

레이아웃: 상단 tier 선택 (`L0`/`L1a`/`L1b`/`L2`/`audit-drip`),
중앙 진행, 하단 이벤트 로그.

audit-drip 모드일 때 추가:
- state file 경로
- throttle (max bytes/s, max wall-clock)
- pause / resume 버튼

### 4.8 Maintenance (`maintenance.py`, PR #12)

진입: backup-set browser 에서 destination 선택 후 `[m]`. 두 가지
운영 작업을 한 화면에서 제공합니다 — 모두 destination 의 캐시된 비밀번호와
이미 열린 백엔드를 재사용하므로 mid-flow 자격증명 재입력 없음.

1. **Rotate keyset password** — 현재/새 비밀번호 입력 후 "Rotate password"
   버튼. 내부적으로 `arq_writer.rotate_keyset_password(blob, old_password,
   new_password)` 가 `<computer-uuid>/encryptedkeyset.dat` 만 재암호화
   ((encryption_key, hmac_key, blob_id_salt) 는 보존). 기존 backuprecord /
   blob 은 그대로 복호화 가능. 작업 후 `CredentialCache` 의 새 비밀번호로
   갱신.
2. **Apply retention** — keep_last_n / keep_daily / keep_weekly /
   keep_monthly / keep_yearly 입력 + "Dry run / Real run" 라디오 +
   "Run blob GC after pruning" 토글. `apply_retention(backend,
   encryption_password=..., policy=RetentionPolicy(...), run_gc=...,
   dry_run=...)` 호출. 콜백 이벤트 (`record_deleted`, `blob_deleted`,
   `pack_deleted`) 가 화면 하단 로그 패널에 스트림.

두 작업 모두 sibling 스레드에서 실행되며 결과는 `call_from_thread` 로
이벤트 루프에 marshal — UI 가 응답성을 잃지 않습니다.

## 5. 진행 콜백 통합

라이브러리는 이미 `ProgressCb(kind: str, payload: dict)` 모델을
일관되게 사용 중입니다 (writer / reader / validator 모두).

### 5.1 Worker thread → UI 브리지

```python
# arq_tui/workers.py
async def run_backup(plan, password, app):
    def callback(kind, payload):
        # textual.App.call_from_thread is thread-safe to invoke
        # from any worker.
        app.call_from_thread(app.post_message, BackupEvent(kind, payload))
    result = await asyncio.to_thread(
        arq_writer.build_backup,
        source=plan.sources[0],
        dest_root=plan.dest,
        encryption_password=password,
        callback=callback,
        # ... + chunker_config, dedup_against_existing 등
    )
    app.post_message(BackupFinished(result))
```

`BackupEvent` / `BackupFinished` 는 Textual `Message` 서브클래스.
관련 화면이 `on_backup_event` 핸들러로 받아서 reactive 속성을 갱신.

### 5.2 Reactive 진행 위젯

```python
# arq_tui/widgets/progress_panel.py
class ProgressPanel(Widget):
    files_written = reactive(0)
    files_reused = reactive(0)
    bytes_plaintext = reactive(0)
    current_file = reactive("")
    ...

    def on_event(self, kind: str, payload: dict) -> None:
        if kind == "file_written":
            self.files_written += 1
            self.current_file = payload["path"]
            self.bytes_plaintext += payload["size"]
        elif kind == "file_reused":
            self.files_reused += 1
        # ... 등등
```

각 reactive 변경마다 Textual 가 자동으로 위젯 영역 재렌더링.

### 5.3 Throughput / ETA 계산

`workers` 가 1초마다 `ThroughputTick` 메시지를 push.
ProgressPanel 이 deque(60) 윈도우로 EMA 계산.

## 6. 영속 상태

### 6.1 디렉터리

```
~/.config/arq-backup-tui/
├── config.toml             # 글로벌 설정 (theme, keyring 사용 여부)
├── plans/
│   ├── <plan-uuid>.json    # 한 플랜 = 한 파일
│   └── ...
├── recent_destinations.json
└── audit_drip/
    └── <state-file>.json   # validator audit_drip 상태
```

### 6.2 Plan JSON 스키마

PR #12 에서 Advanced 단계 필드를 추가한 후의 최종 형태:

```json
{
  "plan_id": "UUID",
  "name": "home-laptop-to-nas",
  "sources": ["/home/me/Documents", "/home/me/Pictures"],
  "destination_kind": "local",      // "local" | "sftp"
  "destination": {
    "path": "/Volumes/arqbackup1"
  },
  "chunker": "arq_v7_41",           // "default" | "arq_v7_41" | "none"
  "per_source_chunkers": {},        // optional: source path → chunker name
  "use_packs": true,
  "dedup_against_existing": true,
  "exclude_globs": ["*.log", "__pycache__"],
  "exclude_regexes": [],
  "exclude_gitignore_lines": ["build/", "!build/keep.txt"],
  "max_file_bytes": null,           // null = no limit; integer = cutoff
  "use_apfs_snapshot": false,        // macOS only; non-macOS 면 자동 fallback
  "retention": {                    // 빈 dict = 전부 보존
    "keep_last_n": 10,
    "keep_daily": 7,
    "keep_weekly": 0,
    "keep_monthly": 0,
    "keep_yearly": 0
  },
  "last_run_iso": "2026-05-08T03:14:22Z"
}
```

기존 (M3) 플랜 JSON 은 advanced 필드가 빠져 있어도 default-empty 값으로
하위호환 로딩 (PR #12 에서 추가된 `test_legacy_plan_loads_with_default_advanced_fields`
회귀 테스트가 보증).

SFTP destination 의 경우:

```json
"destination_kind": "sftp",
"destination": {
  "host": "u123.your-storagebox.de",
  "port": 23,
  "user": "u123",
  "identity_file": "~/.ssh/id_ed25519",
  "path": "/home/u123/arq"          // remote root (필드명 통일)
}
```

### 6.3 비밀번호 처리

- 메모리에만 보관 (TUI 세션 동안), 디스크 저장 안 함.
- 같은 destination 으로 연속 작업 시 한 번만 prompt — 세션 캐시.
- 옵션: `keyring` 통합 (별도 extra dep, 사용자가 명시적으로 설치).

## 7. 에러 처리

- 라이브러리 예외는 worker 에서 잡아서 `ErrorEvent(message, traceback)`
  메시지로 UI 에 전달.
- Modal 에러 다이얼로그 — confirm 후 이전 화면으로 복귀.
- SFTP 연결 실패는 backoff 재시도 (최대 3 회), 그 후 사용자에게
  재시도 / 취소 선택 제공.
- L2 / audit-drip 같은 장시간 작업의 부분 실패: 해당 record 에
  failure 마크 후 계속 진행 (bisect-friendly).

## 8. 구현 단계 (Milestones)

각 단계가 독립적으로 푸시 가능하고, 단계 종료 시 사용자 가치
일부를 제공합니다.

### M1 — 스켈레톤 (1 일)

- `arq_tui/` 패키지 + Textual App 진입점
- `theming.css` (색상 / 폰트 / 키 바인딩 기본)
- `Home` 화면 (정적 placeholder)
- `pyproject.toml` 의 `tui` extra + `arq-tui` 콘솔 스크립트
- `tests/test_tui_smoke.py` — `pilot` 으로 앱 시작/종료 검증

### M2 — 백업 세트 보기 (2 일)

- `BackupSetListScreen` + `RecordBrowserScreen`
- 로컬 + SFTP destination 모두 지원 (이미 backend-aware)
- `widgets/tree_view.py`
- 비밀번호 prompt modal
- 통합 테스트: 사전 생성 백업 destination 에서 record / 트리 노출
  검증

이 시점에서 사용자는 **"백업 destination 들여다보기"** 가능.

### M3 — 백업 실행 (2 일)

- `PlanWizardScreen` (5 단계)
- `BackupRunScreen` + `progress_panel.py`
- `workers.run_backup` 브리지
- 플랜 저장/로드 (`state.PlanRegistry`)
- 통합 테스트: 합성 source 트리 백업 실행 → progress 이벤트 검증
  → restore 로 round-trip

이 시점에서 **새 백업 만들기 + 실행** 가능.

### M4 — 복원 (1.5 일)

- `RestoreRunScreen` (full-folder + selected-paths)
- record_browser 에서 선택된 노드 → restore 흐름
- 진행 패널 재사용

### M5 — 검증 (1.5 일)

- `ValidateRunScreen` (4 tier + audit-drip)
- audit-drip 의 throttle / state-file UI
- pause / resume

### M6 — 폴리시 (1 일)

- 키바인딩 정리
- 색상 테마 + 다크/라이트 토글
- 에러 다이얼로그 일관화
- 키링 통합 (옵션)
- 빈 상태 메시지, 로딩 스피너 등 마이크로 UX

**총 예상 8–9 일**.

## 9. 테스트 전략

### 9.1 단위 테스트

- 각 화면의 reactive 로직 (이벤트 → state 전이) 를 Textual `pilot`
  으로 headless 검증.
- `state.PlanRegistry` 는 일반 unittest.

### 9.2 통합 테스트

- `tests/tui/test_backup_flow.py`: 플랜 생성 → 실행 → restore 의
  end-to-end. 라이브러리는 실제로 호출, 백업 destination 은 임시
  디렉터리.
- `tests/tui/test_record_browser.py`: 사전 생성 destination 을
  로딩하고 트리 노드 펼치기 + 메타데이터 표시 확인.
- `tests/tui/test_validate_flow.py`: 4 tier 모두 실행 후 결과 화면
  검증.

### 9.3 SFTP 통합

- `tests/tui/test_sftp_destination.py`: SftpBackend 를 mock
  (LocalBackend on temp dir) 로 주입하고 destination_picker → list
  → record browser 흐름 검증.
- 실제 SFTP 서버 테스트는 `tests/test_sftp.py` 패턴을 따라 옵션.

### 9.4 스냅샷 테스트

Textual `pilot.snapshot()` 으로 주요 화면의 ASCII 스냅샷을 저장,
회귀 방지.

## 10. 라이브러리 측 보강 사항 (모두 구현 완료)

TUI 가 임베드하면서 발견된 작은 라이브러리 갭들 — 모두 구현됨:

1. **`Restore.list_records(folder_uuid) -> List[RecordInfo]`** ✅
2. **`Restore.restore(*, backuprecord_path=...)` 옵션** ✅
3. **`Restore.restore(*, paths=[...])` 옵션** ✅
4. **`Backup.cancel()`** ✅
5. **PR #12 추가**: `MaintenanceScreen` 통합을 위해 다음도 추가됨:
   - `arq_writer.rotate_keyset_password(blob, old_password, new_password)`
   - `arq_writer.apply_retention(...)` + `RetentionPolicy`
   - `Backend.unlink()` (LocalBackend + SftpBackend)
   - `arq_writer.with_apfs_snapshot()` + `NotMacOSError` (PR #8)
   - `arq_writer.ExclusionRules` + `Backup(exclusions=..., max_file_bytes=...)`

## 11. 해결된 결정 사항

- **로컬 alongside 한국어 UI?** → 한국어 라벨 + 영어 키바인딩 텍스트 hard-code.
- **mtime / mode 복원?** → 둘 다 ✅ 구현됨 (`os.utime` + `os.chmod`).
- **여러 source 다중 폴더 plan?** → ✅ M3 부터 multi-source 지원
  (`Backup.add_folder` 는 plan 당 multi-folder, wizard 도 다중 source 입력 받음).
- **Plan 편집 / 삭제 UI?** → 삭제는 ✅ (`arq-tui plans delete`), 편집은
  v1.x 로 deferred (recreate via wizard + delete 권장; 직접 JSON 편집 가능).

## 12. 보안 / 개인정보 노트

- 비밀번호: 디스크 미저장. 메모리 유지 시 `bytes` 로만 사용,
  `str` 단위 캐싱은 escape 시점에 즉시 `del`.
- 로그 / 화면 캡처: `payload` 내 `path` 가 사용자 파일 경로를 노출
  하므로 스냅샷 테스트 시 항상 redact.
- SFTP keyring 을 활성화한 경우 사용자 OS 인증으로 잠금 — TUI 가
  자체 인증 로직을 추가하지 않음.

---

본 계획서는 사용자 승인 후 M1 부터 순차 구현. 각 마일스톤 종료
시점마다 별도 commit + push, COVERAGE.md 의 TUI 행을 ❌ → ✅ 로
점진적 업데이트.
