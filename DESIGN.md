# arq-backup-tui — DESIGN

본 문서는 현재까지 합의·구현된 프로젝트의 목표·구조·설계 결정사항을
기록합니다. 향후 변경은 PR 기반으로 본 문서에 함께 반영합니다.

## 1. 프로젝트 목표

`arq-backup-tui` 는 [Arq Backup](https://www.arqbackup.com/) 7 포맷의
백업 대상지를 **공식 Arq.app 없이도** 다룰 수 있게 하는 TUI 애플리케이션을
지향합니다. 현재 단계에서는 그 첫 빌딩블록인 **독립 검증기(Validator)**
를 라이브러리 + CLI 형태로 제공합니다.

### 1.1 운영자 입장의 사용 시나리오

- **오프-사이트 백업의 무결성을 Arq.app 의 월간 자체 검증보다 빠르게**
  확인 (수일~수주 단위로 비트로트 / 일부 전송 / 구조 손상 감지).
- 로컬 미러(예: `/Volumes/arqbackup1`)와 원격 SFTP 대상지(예: Hetzner
  Storage Box) 양쪽에 동일한 검증 로직 적용.
- 향후 단계에서는 TUI 로 백업 상태를 시각화하고, 검증·복구·생성까지
  통합한 운영자 도구로 확장.

### 1.2 본 저장소가 상속한 reference 출처

`scripts/arq-validate.py` (in `neoocean/docker-monitor`, 2,673 LOC)
가 운영자 환경에 특화되어 작성되어 있던 독립 검증기입니다. 본 프로젝트는
이 reference 의 검증 로직·포맷 해석·경험적 보정값 등을 받아들이되,
TUI/라이브러리에 적합하도록 다음과 같이 재편했습니다:

| 항목 | reference (`docker-monitor`) | 본 프로젝트 |
| --- | --- | --- |
| 패키징 | 단일 파일 스크립트 | `arq_validator/` 패키지 |
| 비밀 저장소 | 파일 기반 `.secrets/` 강제 | TUI/CLI 가 호출 시 인자로 전달 |
| 진행 상황 | stderr 텍스트 | `ProgressCallback` 이벤트 + stderr |
| Hetzner 특화 | 연결 속도 제한 감지 등 내장 | 백엔드 추상화 (다른 대상지에 재사용 가능) |
| 백엔드 | SFTP 전용 + 로컬 변형 | `Backend` 프로토콜 + `LocalBackend` / `SftpBackend` |
| audit-drip | 운영자 LaunchAgent 결합 | TUI/CLI/스케줄러 어디서든 호출 가능 |

reference 의 핵심 reverse-engineering 결과 — 25바이트 패딩 없는 keyset
매직, 32바이트(공식 스펙은 64바이트) 키 필드, ARQO 멀티오브젝트 컨테이너
탐지 등 — 는 본 프로젝트에서도 그대로 채택했습니다. (`arq_validator/
constants.py` 의 주석 참고.)

## 2. 패키지 구조

```
arq-backup-tui/
├── arq_validator/                # 검증 라이브러리 (TUI 가 import)
│   ├── __init__.py               # 공개 API 노출
│   ├── __main__.py               # `python -m arq_validator`
│   ├── constants.py              # Arq 7 포맷 상수 (regex, magic, offset 등)
│   ├── crypto.py                 # PBKDF2 / HMAC / openssl AES-256-CBC
│   ├── backend.py                # Backend Protocol + LocalBackend
│   ├── sftp.py                   # SftpBackend (OpenSSH 클라이언트 래핑)
│   ├── layout.py                 # 디렉토리 디스커버리 + 백업레코드 탐색
│   ├── events.py                 # ProgressCallback + Event 정의
│   ├── tiers.py                  # L0/L1a/L1b/L2 검증 함수
│   ├── audit_drip.py             # 재개형 야간 감사 (cursor + throttle)
│   ├── machine_info.py           # source 머신 식별 (backupconfig.json + backupplan.json + 호스트 비교)
│   ├── runner.py                 # ValidationTier enum + validate() 오케스트레이터
│   └── cli.py                    # argparse CLI
├── arq_writer/                   # 백업 생성 라이브러리 (Arq.app 호환)
│   ├── __init__.py               # 공개 API 노출
│   ├── __main__.py               # `python -m arq_writer`
│   ├── constants.py              # 압축 타입, Tree 버전 등 (validator 상수 재export)
│   ├── lz4_block.py              # 순수 파이썬 LZ4 블록 codec
│   ├── types.py                  # BlobLoc / FileNode / TreeNode / Tree 데이터클래스
│   ├── serialize.py              # Node / Tree / BlobLoc 바이너리 직렬화
│   ├── crypto_write.py           # ARQO 인코더 + encryptedkeyset.dat 빌더 + AES 암호화 + rotate_keyset_password
│   ├── json_configs.py           # backupconfig / backupplan / backupfolders 빌더
│   ├── backuprecord.py           # backuprecord plist + LZ4 + ARQO 파이프라인
│   ├── pack_builder.py           # Arq 7 PackBuilder — treepacks/blobpacks 생성 (use_packs=True 모드)
│   ├── chunker.py                # Buzhash 콘텐츠 정의 청커 + 다중 버전 레지스트리
│   ├── arq_chunker_params.py     # Arq.app v7.41 RE 파라미터 + ChunkerConfig 레지스트리
│   ├── chunker_oracle.py         # 청커 선택 휴리스틱 (size-based fallback)
│   ├── prior_tree_index.py       # PriorTreeIndex — tree-walk dedup 캐시 시드
│   ├── dedup.py                  # dedup-against-existing blob 캐시 빌더
│   ├── exclusions.py             # ExclusionRules (glob + regex + .gitignore 파싱)
│   ├── macos_snapshot.py         # macOS APFS 스냅샷 지원 (with_apfs_snapshot, is_macos*)
│   ├── retention.py              # RetentionPolicy + prune_records + gc_orphan_blobs + apply_retention
│   ├── macho_buzhash_finder.py   # Arq.app Mach-O 정적 분석 + 청크 크기 행동 추론
│   ├── buzhash_re_cli.py         # `arq-buzhash-find` CLI
│   ├── backup.py                 # Backup 클래스 + build_backup() 오케스트레이터
│   └── cli.py                    # argparse CLI (`arq-backup create`, 8개 신규 플래그 포함)
├── arq_reader/                   # 백업 복원 라이브러리 (writer 의 역방향)
│   ├── __init__.py
│   ├── __main__.py               # `python -m arq_reader`
│   ├── decrypt.py                # ARQO 전체 복호화 (HMAC verify 후 AES 2단)
│   ├── parse.py                  # BinaryReader + Arq 7 Node/Tree/BlobLoc 바이너리 파서
│   ├── restore.py                # Restore 클래스 (standalone + isPacked=true 양쪽 지원)
│   ├── arq5_pack.py              # Arq 5/6 .pack/.index 파서 + 빌더 (SHA-1 fanout 포함)
│   ├── arq5_binary.py            # Arq 5/6 Tree v10-v22 / Commit v3-v12 / Node / BlobKey 바이너리 파서
│   ├── arq5_keyset.py            # Arq 5/6 encryptionvN.dat 복호화 (PBKDF2-SHA1)
│   ├── arq5_restore.py           # Arq 5/6 백업 복원 오케스트레이터 (commit→tree→files)
│   └── cli.py                    # argparse CLI (`arq-reader list`/`restore`)
├── arq_tui/                      # Textual TUI (writer + reader + validator 통합)
│   ├── __init__.py               # ArqTuiApp 노출
│   ├── __main__.py               # `python -m arq_tui` 진입점
│   ├── app.py                    # 최상위 앱 (PlanRegistry, CredentialCache, DestinationStore)
│   ├── state.py                  # Plan / Destination 데이터클래스 + 영속 저장소
│   ├── workers.py                # BackupWorker / RestoreWorker / ValidateWorker (in-process worker thread bridge)
│   ├── runs.py                   # State-file IPC: RunWriter / enumerate_runs / signal_cancel / gc
│   ├── console_commands.py       # Slash-command dispatch for the quake-style console
│   ├── backend_open.py           # 백엔드 open/close (LocalBackend / SftpBackend)
│   ├── cli.py                    # `plans` / `runs` / `machine-info` 헤드리스 서브커맨드
│   ├── theming.css               # 색상·여백 등 CSS
│   ├── screens/
│   │   ├── home.py               # 랜딩 (플랜 목록 + 빠른 액션)
│   │   ├── plan_wizard.py        # 6단계 마법사 (sources / dest / enc / chunker / advanced / review)
│   │   ├── backup_run.py         # 실행 + ProgressPanel
│   │   ├── backup_sets.py        # destination/layout 브라우저 (밑에서 [m] 으로 maintenance 진입)
│   │   ├── record_browser.py     # 단일 backuprecord 트리 워크
│   │   ├── restore_run.py        # 복원 실행 + ProgressPanel
│   │   ├── validate_run.py       # 검증 실행 + ProgressPanel
│   │   ├── maintenance.py        # 비밀번호 회전 + retention 적용
│   │   ├── runs_monitor.py       # Activity 화면 — 외부 프로세스의 state file 을 1Hz polling
│   │   └── help.py
│   └── widgets/
│       ├── source_picker.py / destination_modal.py
│       ├── password_modal.py / restore_target_modal.py
│       ├── console.py            # Quake-style slash-command console (slide-down)
│       └── progress_panel.py
├── tests/                        # 합성/round-trip 단위·통합 테스트 (355건, ~140초; 7건 skip = SFTP 자격증명 의존)
│   ├── fixtures.py               # 검증기 테스트용 Arq 7 트리 빌더
│   ├── integration/              # 실제 Arq.app SFTP destination 호환성 검증 (.env 기반)
│   │   ├── _creds.py             # 환경변수 + .env 자격증명 로더
│   │   └── test_arqapp_sftp_compat.py
│   ├── test_crypto.py / test_layout.py / test_runner.py
│   ├── test_audit_drip.py / test_sftp.py
│   ├── test_writer_lz4.py        # 순수 LZ4 codec round-trip
│   ├── test_writer_format.py     # 바이너리 직렬화 + crypto round-trip
│   ├── test_writer_e2e.py        # 작성기 → 검증기 4단계 round-trip
│   ├── test_writer_packed.py     # packed 모드 (treepacks/blobpacks) round-trip
│   ├── test_writer_chunker.py    # Buzhash 청커 round-trip
│   ├── test_writer_dedup.py      # cross-run dedup 검증
│   ├── test_writer_tree_walk_reuse.py # PriorTreeIndex 기반 walk-reuse 검증
│   ├── test_writer_exclusions.py # ExclusionRules glob/regex/gitignore
│   ├── test_writer_cli_flags.py  # arq-backup CLI 8개 신규 플래그
│   ├── test_retention.py         # RetentionPolicy + prune + GC round-trip
│   ├── test_fingerprint.py       # 형상 지문 호환성 검증
│   ├── test_reader_e2e.py        # Reader byte-identical 복원 검증
│   └── test_tui_m{1..7}_*.py     # TUI 단계별 smoke + 기능 테스트
├── docs/
│   ├── DESIGN.md                                  # ← 본 문서 위치는 repo 루트
│   ├── COMPATIBILITY.md / COVERAGE.md / GUI-PARITY.md
│   ├── MECHANISM.md / PLAN-tui.md
│   ├── COMPAT-VERIFICATION.md / COMPAT-SFTP-TESTING.md
│   ├── APFS-SNAPSHOTS.md / UNICODE.md
│   ├── RESEARCH-backup-creation-feasibility.md    # 작성 전 타당성 (구현 완료)
│   └── RESEARCH-format-extensions.md              # pack/청커/Arq5 RE 노트 (대부분 구현 완료)
├── arq-tui.py                    # 루트 진입점 (./arq-tui.py 로 TUI 실행)
├── pyproject.toml                # 콘솔 스크립트 등록
├── DESIGN.md                     # ← 본 문서
└── LICENSE
```

## 3. 검증 계층 (Tier) 모델

Arq.app 의 자체 검증 계층과 일대일 매핑되는 4단계 검증을 제공합니다.
각 계층은 그 아래 모든 계층을 포함합니다.

| Tier | 이름 | 무엇을 보는가 | 비용 | 빈도 (운영자 권장) |
| --- | --- | --- | --- | --- |
| **L0** | `dry-run` | 디렉토리 모양 (computer UUID, 4개 객체 패밀리, backupfolders) + keyset 존재 | I/O ~수백 ms | 실시간 |
| **L1a** | `quick` | ARQO 매직바이트 표본 sweep (기본 5%, `--sample-fraction 1.0` 으로 전수) | 표본 × 4바이트 RTT | 주간 |
| **L1b** | `deep` | `encryptedkeyset.dat` 복호화 + 백업폴더별 최신 backuprecord HMAC | keyset 1회 + 폴더당 ≤50 MB | 분기/90일 |
| **L2** | `audit` | 모든 EncryptedObject HMAC (멀티오브젝트 컨테이너 인식) | 객체 전수 다운로드 | 연 1회 또는 의심 시 |

L2 는 대상지가 클 경우 한 번에 수 시간 걸리므로 별도의 **audit-drip**
모드를 제공합니다 (§ 5).

### 3.1 검증이 잡아내는 결함 유형

- **L0**: 마운트 누락, 잘못된 root 경로, 컴퓨터 UUID 누락
- **L1a**: 부분 전송으로 인한 잘림, 파일 교체, 구조적 손상
- **L1b**: 비밀번호 오류, keyset 손상, 최신 백업 메타데이터의
  비트로트
- **L2**: 모든 객체에 대한 비트로트 / 변조 / 암호화 시 손상 (Arq 의
  월간 자체 검증과 동일한 보장)

## 4. 핵심 추상화

### 4.1 Backend 프로토콜 (`backend.py`)

```python
class Backend(Protocol):
    def list_dir(self, path: str) -> list[str]: ...
    def stat_size(self, path: str) -> int: ...
    def read_range(self, path: str, offset: int, length: int) -> bytes: ...
    def read_all(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...
    def is_dir(self, path: str) -> bool: ...
```

검증 로직은 이 6개 메서드만 의존합니다. 따라서 새 백엔드(S3, B2, WebDAV
등)를 추가하려면 해당 클래스만 구현하면 됩니다.

#### 동봉되는 구현체

- **`LocalBackend(root_path)`**: 로컬 파일시스템. 경로 탈출(`..`) 방어
  내장.
- **`SftpBackend(host, port, user, password|identity_file, ...)`**:
  OpenSSH `ssh -N -M` 마스터 + ControlPath 멀티플렉싱. 패스워드 인증은
  SSH_ASKPASS shim (argv 노출 없음). `read_range` 는 offset 0 일 때
  `head -c N`, 아니면 `dd bs=1 skip=K count=N status=none` 사용 — Hetzner
  의 제한된 셸에서도 동작 확인된 조합.

### 4.2 Crypto 전략 (`crypto.py`)

- **PBKDF2-SHA256, HMAC-SHA256**: Python stdlib (`hashlib`, `hmac`)
- **AES-256-CBC**: `openssl` CLI subprocess 만 사용
- **Python 서드파티 crypto 패키지 의존성 0개** — `cryptography`,
  `pycryptodome` 등 모두 미사용. `openssl` 은 macOS / Linux 모두에 표준
  탑재된 점, 그리고 AES 호출이 keyset 복호화 1회뿐이라는 점에서 합리적
  trade-off.
- ARQO HMAC 검증은 stdlib 만으로 가능하므로 `openssl` 없이도 L0/L1a/L2
  의 핵심 (HMAC) 부분은 동작.

### 4.3 진행 이벤트 (`events.py`)

```python
class EventKind(Enum):
    RUN_STARTED, RUN_FINISHED, TIER_STARTED, TIER_FINISHED,
    LAYOUT_DISCOVERED, COMPUTER_FOUND,
    MAGIC_CHECK_PROGRESS, MAGIC_CHECK_FAILED,
    KEYSET_DECRYPTED, KEYSET_FAILED,
    BACKUPRECORD_VERIFIED, BACKUPRECORD_FAILED,
    AUDIT_FILE_VERIFIED, AUDIT_FILE_FAILED, AUDIT_FILE_SKIPPED,
    AUDIT_PROGRESS,
    AUDIT_DRIP_FIRE_STARTED, AUDIT_DRIP_FIRE_FINISHED,
    AUDIT_DRIP_SWEEP_STARTED, AUDIT_DRIP_SWEEP_COMPLETED,
    AUDIT_DRIP_PROGRESS, AUDIT_DRIP_ABORTED, AUDIT_DRIP_PAUSED,
    LOG, ...

@dataclass
class Event:
    kind: EventKind
    message: str
    payload: dict

ProgressCallback = Callable[[Event], None]
```

TUI 는 `validate(..., callback=on_event)` 로 콜백을 넘기면 라이브
진행상황을 받습니다. 콜백 안에서 발생한 예외는 `events.emit()` 에서 흡수
— UI 핸들러가 throw 해도 검증 루프는 멈추지 않습니다.

### 4.4 검증 오케스트레이터 (`runner.py`)

```python
class ValidationTier(Enum):
    DRY_RUN = "dry-run"   # L0 only
    QUICK   = "quick"     # L0 + L1a magic sweep
    DEEP    = "deep"      # + L1b backuprecord HMAC
    AUDIT   = "audit"     # + L2 full HMAC sweep

@dataclass
class ValidationReport:
    tier: str
    started_at, finished_at: float
    layout: LayoutResult | None
    magic_check: MagicCheckResult | None
    backuprecord: BackupRecordResult | None
    audit: ObjectAuditResult | None
    error: str | None
    def has_failures(self) -> bool: ...
    def to_dict(self) -> dict: ...

def validate(backend, *, tier, root="/", encryption_password=None,
             sample_fraction=0.05, audit_skip_larger_than=...,
             callback=None, ...) -> ValidationReport: ...
```

## 5. audit-drip — 재개형 L2 감사

L2 는 대상지가 클 경우 단일 fire 로 끝낼 수 없습니다 (수십~수백 GB).
`audit_drip.run_audit_drip()` 은 **30분 ~1시간 야간 budget** 으로 매일
조금씩 검사를 진척시키는 nightly-fire 모델을 구현합니다.

### 5.1 워크 순서

```python
walk = [
    (computer_uuid, family, shard, file_name)
    for computer_uuid in sorted(computers)
    for family in ("blobpacks", "treepacks", "largeblobpacks", "standardobjects")
    for shard, file_name in sorted(items_in(family, computer_uuid))
]
```

순서가 결정적이므로 fire 사이에 디렉토리가 늘어나거나 줄어도 커서가
의미를 유지합니다.

### 5.2 커서 + 재개

- 매 파일 처리(성공/실패/에러/스킵 무관) 직후 커서 갱신:
  `(cursor_computer, cursor_kind, cursor_shard, cursor_file_name)`
- 다음 fire 는 커서보다 **사전식으로 큰** 첫 항목부터 시작 — 커서 위치의
  파일이 사라져도 안전하게 다음으로 진행.
- 워크 끝까지 도달하면 `sweep_completed_at` 기록, 다음 fire 가 새 sweep
  시작 (`sweep_count += 1`).

### 5.3 부드러운 한도

| 옵션 | 의미 | 기본값 |
| --- | --- | --- |
| `max_runtime_sec` | fire 1회당 wall-clock 예산 | 0 (무제한) |
| `rate_files_per_min` | 파일 간 최소 간격 (Throttle) | None (제한 없음) |
| `paused_until_epoch` | silent skip 까지의 epoch | None |
| `skip_larger_than` | 이 크기 초과는 검증 생략 | 256 KB (Arq `maxPackedItemLength`) |

`failed_files_this_sweep` 은 100건 상한이 있어 폭주 corruption 이
state 파일을 부풀리지 않습니다.

### 5.4 상태 파일

```jsonc
{
  "target": "hetzner",
  "sweep_started_at": 1715000000.0,
  "sweep_completed_at": null,
  "sweep_count": 3,
  "cursor_computer": "12345678-...",
  "cursor_kind": "blobpacks",
  "cursor_shard": "ff",
  "cursor_file_name": "0000FF-...-...pack",
  "files_audited_this_sweep": 12345,
  "files_total_this_sweep": 462000,
  "fails_this_sweep": 0,
  "errors_this_sweep": 0,
  "failed_files_this_sweep": [],
  "last_fire_aborted_reason": "max_runtime",
  "paused_until_epoch": null,
  "error": null
}
```

대상별로 별도 파일을 사용하므로 (`./arq_audit_drip_local.json`,
`./arq_audit_drip_hetzner.json`) 로컬·원격 sweep 이 동시에 진행되어도
충돌 없습니다.

## 6. CLI (`arq_validator.cli`)

```
arq-validator <tier> [path] [options]

tier:
  dry-run | quick | deep | audit | audit-drip

path:
  로컬 모드의 백업 루트. --sftp 사용 시 생략.

옵션:
  # 비밀번호 (deep/audit/audit-drip 필수)
  --password / --password-file / --password-env

  # SFTP 대상지
  --sftp user@host[:port]:/root
  --sftp-password / --sftp-password-env / --sftp-password-file
  --sftp-identity-file
  --sftp-known-hosts

  # tier-별 노브
  --sample-fraction 0.05   # quick/deep/audit
  --audit-skip-larger-than 256000
  --audit-max-runtime-sec / --audit-max-bytes

  # audit-drip
  --target {free-form label}
  --state-file ./arq_audit_drip_<target>.json
  --max-runtime-sec / --rate-files-per-min

  # 출력
  --quiet / --json-events
```

종료 코드:

- `0` — 검증 통과
- `2` — 호출 오류 (인자, 경로, 비밀번호 누락)
- `3` — 백엔드/IO 오류
- `4` — 검증 실패 또는 audit-drip 에 실패 항목 있음

## 7. 테스트 전략

`tests/fixtures.py` 가 합성 Arq 7 트리를 생성합니다 — 진짜 Arq 백업
없이도 모든 계층을 round-trip 검증합니다.

- **합성 키셋**: 알려진 비밀번호 + 무작위 키 → `encryptedkeyset.dat`
  바이트를 빌드 → 검증기로 복호화 round-trip
- **합성 ARQO**: HMAC 가 valid 한 ARQO 객체를 만들고 단일/멀티 컨테이너,
  손상 케이스(바이트 플립) 등으로 실패 경로 모두 커버
- 355건 테스트 (~140초; 7건 skip = 실 SFTP 자격증명 의존). 실제 SFTP 서버는
  sandbox 에 없으므로 기본 단위 테스트는 생성 검증·spec 파서·`__enter__` 이전
  호출 차단 contract 만 검증하고, **운영자가 `.env` 자격증명으로 실 destination
  대상 통합 테스트** 를 별도 실행할 수 있는 harness (`tests/integration/`,
  `docs/COMPAT-SFTP-TESTING.md`) 를 제공합니다.

## 8. 의존성·실행 환경

- **런타임**: Python ≥ 3.9 + `openssl` CLI (PATH 또는 `--openssl-path`)
- **SFTP 사용 시**: 시스템의 OpenSSH `ssh` / `sftp` 클라이언트
- **Python 서드파티 패키지**: 없음 (런타임/테스트 모두 stdlib 만)
- **OS 검증**: macOS, Linux. 윈도우는 미지원 (OpenSSH/openssl 의 동작
  세부가 다름).

## 9. 이미 구현 완료된 확장 (PR #5–#12)

DESIGN.md 의 초기 버전에서는 청커, pack 컨테이너, TUI, 보존 정책 등이
deferred 로 표기되어 있었으나 이후 PR 시리즈로 모두 구현되었습니다.
역사적 기록은 `docs/RESEARCH-format-extensions.md` 와
`docs/RESEARCH-backup-creation-feasibility.md` 에 보존되어 있고,
현 시점 상태 요약은 다음과 같습니다.

### 9.1 백업 작성 고급 기능

| 기능 | PR | 활성화 방법 |
| --- | --- | --- |
| Buzhash content-defined chunking | #5 | `Backup(chunker_config=...)` / CLI `--chunker {none\|default\|arq_v7_41}` |
| Arq.app v7.41 RE 청커 파라미터 | #5 | `arq_chunker_params.ARQ_V7_CHUNKER_CONFIG` |
| Pack mode (treepacks/blobpacks) | #5 | `Backup(use_packs=True)` / CLI `--use-packs` |
| Cross-run dedup | #5 | `Backup(dedup_against_existing=True)` / CLI `--dedup-against-existing` |
| Tree-walk reuse (`PriorTreeIndex`) | #5 | dedup-against-existing 활성화 시 자동 |
| `ExclusionRules` (glob/regex/.gitignore) | #10 | `Backup(exclusions=...)` / CLI `--exclude-glob/--exclude-regex/--exclude-from` |
| max-file-bytes 컷오프 | #10 | `Backup(max_file_bytes=N)` / CLI `--max-file-bytes` |
| macOS APFS 스냅샷 | #8 | `with_apfs_snapshot()` / CLI `--use-apfs-snapshot` |

### 9.2 유지보수 기능

| 기능 | PR | 진입점 |
| --- | --- | --- |
| `RetentionPolicy` (keep_last_n + 시간 버킷) | #11 | `apply_retention(backend, policy=...)` |
| `prune_records()` (백업레코드 가지치기) | #11 | retention 의 첫 단계 |
| `gc_orphan_blobs()` (보수적 pack 단위 GC) | #11 | retention 의 두 번째 단계 |
| `Backend.unlink()` (Local + Sftp) | #11 | retention/gc 가 호출 |
| `rotate_keyset_password()` (비밀번호 변경) | #5/#7 | 마스터 키 보존, salt+IV+ciphertext+HMAC 만 재생성 |

### 9.3 TUI 통합

| 기능 | PR | 위치 |
| --- | --- | --- |
| `arq_tui/` 패키지 (M1–M6) | (M-시리즈) | Home / wizard / backup-set browser / record browser / restore / validate |
| Plan wizard "Advanced" 단계 (6단계) | #12 | exclusions / max-file-bytes / APFS / retention 모두 노출 |
| `MaintenanceScreen` (`[m]`) | #12 | 비밀번호 회전 + retention 적용 + dry-run/real-run + GC 토글 |
| 루트 `arq-tui.py` 진입점 | #12 | `./arq-tui.py` 로 즉시 실행 (sys.path 자가 삽입) |
| `Plan` 데이터클래스 신규 필드 | #12 | `exclude_globs` / `exclude_regexes` / `exclude_gitignore_lines` / `max_file_bytes` / `use_apfs_snapshot` / `retention` |

### 9.4 호환성 검증

| 기능 | PR | 위치 |
| --- | --- | --- |
| Shape fingerprint 헬퍼 | #7 | `tests/test_fingerprint.py` (salt-독립 구조 비교) |
| 실 Arq.app SFTP 통합 테스트 harness | #9 | `tests/integration/`, `.env.example`, `docs/COMPAT-SFTP-TESTING.md` |

## 10. 향후 작업 (현재 미구현)

다음은 본 문서 범위에서 의식적으로 **deferred** 로 둔 항목들입니다.

### 10.1 추가 백엔드

S3, Backblaze B2, WebDAV, dropbox 등. `Backend` 프로토콜의 7개 메서드
(`list_dir`/`stat_size`/`read_range`/`read_all`/`exists`/`is_dir`/`unlink`
+ writer 가 쓰는 `mkdir`/`write_all`) 만 구현하면 기존 로직 전체 재사용 가능.

### 10.2 작성기 — 백업 생성 (배경)

`arq_writer/` 패키지가 v0 백업 작성기를 제공합니다 — 조사 결과
(`docs/RESEARCH-backup-creation-feasibility.md`) 에서 권장한
"청커·pack 컨테이너 우회, 모든 객체를 standalone EncryptedObject 로
`standardobjects/<shard>/<blobid>` 에 저장" 전략을 채택했습니다.

#### 작성기 동작 흐름

1. 무작위 32바이트 `encryption_key` / `hmac_key` / `blob_id_salt`
   생성 → `encryptedkeyset.dat` (PBKDF2-SHA256 / AES-256-CBC / HMAC)
2. 4개 root JSON 작성 (`backupconfig.json`, `backupplan.json`,
   `backupfolders.json`, 폴더당 `backupfolder.json`)
3. 소스 디렉토리 재귀 walk:
   - 파일: 내용 → LZ4 wrap → ARQO 암호화 → `standardobjects/<2hex>/<62hex>`
     (blob_id = `SHA-256(blob_id_salt || plaintext)`)
   - 디렉토리: 자식 노드 수집 → Tree 바이너리 직렬화 → 위와 동일하게 저장
4. 루트 TreeNode 를 backuprecord plist (binary plist) 에 임베드 →
   LZ4 wrap → ARQO 암호화 → `backupfolders/<folder>/backuprecords/<NNNNN>/<num>.backuprecord`

byte-identical 파일은 SHA-256 blob_id 가 같아 자연 dedup. modified-in-place
파일은 청커가 없어 dedup 되지 않음 (운영자 도구 입장에서 acceptable).

#### 호환성 검증 상태

| Verdict | 상태 | 근거 |
| --- | --- | --- |
| **A**: 본 검증기로 round-trip | ✅ 통과 | `tests/test_writer_e2e.py` 가 4단계 모두 검증 (dry-run / quick / deep / audit) |
| **A'**: 본 reader 로 byte-identical 복원 | ✅ 통과 | `tests/test_reader_e2e.py` — 작성기로 만든 백업을 reader 로 복원 후 `diff -r` 통과 |
| **B**: arq_restore (BSD) round-trip | ⚠️ 미검증 | arq_restore 는 macOS 전용 API (CommonCrypto, Security framework, Mach 헤더, 애플 xattr API) 의존 — Linux 포팅은 다일 작업으로 추정. 운영자 macOS 환경에서 직접 검증 필요 |
| **C**: Arq.app GUI 복원 | ⚠️ 미검증 | macOS GUI 가 필요한 수동 검증 |

본 reader 의 byte-identical 복원은 writer 의 모든 바이너리 포맷이
스펙·검증기·reader 3자 간 일치함을 자체 보증합니다. arq_restore
는 동일한 공식 스펙으로 작성되었으므로 reader 가 통과하면 arq_restore
도 통과할 가능성이 매우 높지만, 형식적 보증은 macOS 빌드 후 직접
확인 필요.

작성기가 생성한 binary plist + LZ4 + ARQO 모든 레이어를 직접 풀어
plist 키들(`archived`, `arqVersion`, `node`, `treeBlobLoc.blobIdentifier`
등)이 스펙과 일치하는지 확인하는 테스트
(`test_backuprecord_decrypts_and_parses_as_plist`)도 통과합니다.

#### 알려진 한계 (현재 시점)

- **windowsattrs / xattr / ACL 메타데이터 0 으로 채움**: 기본 동작은
  파일 내용 + 기본 stat 만 보존. 필요해지면 노드 빌더 확장 가능.
- **macOS 외 OS 의 일관성 스냅샷**: APFS 외 (Linux btrfs/LVM, Windows VSS) 의
  스냅샷은 미지원. macOS 에서는 `--use-apfs-snapshot` 으로 frozen-source 백업 가능.

> 이전에 한계로 표기되었던 **청커**와 **pack 컨테이너**는 PR #5 에서 구현되어
> CLI `--chunker {none|default|arq_v7_41}` / `--use-packs` 로 활성화됩니다.
> Arq.app v7.41 파라미터는 Mach-O RE (`macho_buzhash_finder.py`) 로 도출했습니다.
> 자세한 구현 상태는 §10 "이미 구현 완료된 확장" 참조.

### 10.3 Hetzner 특화 안전장치

reference 가 가진 connection-rate-limit 자동 감지(`Connection refused`,
`mux_client_request_session` 패턴 추적, 20회 연속 실패 시 조기 abort) 는
SftpBackend 에 아직 포팅되지 않았습니다. 운영자가 Hetzner 외 대상도
사용하는 경우 일반화된 형태로 추가 예정.

### 10.4 Arq 5/6 작성 (write-side)

현재 Arq 5/6 은 **읽기/복원만** 지원합니다 (`arq_reader/arq5_*.py`).
Arq 5/6 형식으로 백업을 생성하는 작성기는 미구현. Arq.app 자체가
새 백업은 모두 Arq 7 형식으로 생성하므로 우선순위가 낮음.

### 10.5 일관성 스냅샷 — macOS 외 OS

현재는 macOS APFS 만 지원합니다 (`with_apfs_snapshot()`). Linux btrfs /
LVM thin / ZFS, Windows VSS 는 아직 미구현. 운영자 환경의 파일시스템 종류
에 따라 단계적으로 추가 예정.

### 10.6 스케줄링·자동 실행

retention 정책의 자동 적용, audit-drip 의 cron / launchd 통합, 백업의
주기적 실행 등은 현재 모두 운영자 수동 호출. policy 레이어로 별도 PR
에서 추가 예정.

## 11. 참고 자료

- Arq 7 데이터 포맷 (공식): <https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html>
- Arq 5 포맷 (재사용된 PBKDF2/HMAC 규칙 출처): <https://www.arqbackup.com/arq_data_format.txt>
- 본 검증기의 reference 구현: `neoocean/docker-monitor` →
  `scripts/arq-validate.py` (2,673 LOC)
- Reverse-engineering 보정값의 일차 출처: 운영자 환경 (Hetzner Storage
  Box) 에서의 실측 (`docker-monitor` SCENARIO §13, 2026-05-04 ~
  2026-05-05)
