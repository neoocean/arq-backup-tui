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
├── arq_validator/                # 라이브러리 본체 (TUI 가 import)
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
│   ├── runner.py                 # ValidationTier enum + validate() 오케스트레이터
│   └── cli.py                    # argparse CLI
├── tests/                        # 합성 fixture 기반 단위·통합 테스트 (47건)
│   ├── fixtures.py               # 테스트용 Arq 7 트리 빌더
│   ├── test_crypto.py
│   ├── test_layout.py
│   ├── test_runner.py
│   ├── test_audit_drip.py
│   └── test_sftp.py
├── docs/
│   └── RESEARCH-backup-creation-feasibility.md   # (별도 조사 진행 중)
├── pyproject.toml                # `arq-validator` 콘솔 스크립트 등록
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
- 47건 테스트 (~9초). 실제 SFTP 서버는 sandbox 에 없으므로 SFTP 테스트는
  생성 검증·spec 파서·`__enter__` 이전 호출 차단 contract 만 검증.

## 8. 의존성·실행 환경

- **런타임**: Python ≥ 3.9 + `openssl` CLI (PATH 또는 `--openssl-path`)
- **SFTP 사용 시**: 시스템의 OpenSSH `ssh` / `sftp` 클라이언트
- **Python 서드파티 패키지**: 없음 (런타임/테스트 모두 stdlib 만)
- **OS 검증**: macOS, Linux. 윈도우는 미지원 (OpenSSH/openssl 의 동작
  세부가 다름).

## 9. 향후 작업 (현재 미구현)

다음은 본 문서 범위에서 의식적으로 **deferred** 로 둔 항목들입니다.

### 9.1 TUI 자체

`textual` 등을 사용한 TUI 위젯은 별도의 PR/모듈에서. 현재 라이브러리는
`ProgressCallback` 으로 TUI 통합 지점만 노출.

### 9.2 추가 백엔드

S3, Backblaze B2, WebDAV, dropbox 등. `Backend` 프로토콜의 6개 메서드만
구현하면 기존 검증 로직 전체 재사용 가능.

### 9.3 백업 생성 (write-side)

`arq-backup-tui` 가 Arq.app 처럼 백업을 **생성** 할 수 있는지 조사한
결과는 `docs/RESEARCH-backup-creation-feasibility.md` 에 정리되어
있습니다. 요약하면:

- **공개된 부분**: `encryptedkeyset.dat`, `EncryptedObject`/ARQO 헤더,
  `Node`/`Tree`/`BlobLoc` 바이너리 레이아웃, JSON 설정 파일들 — 모두
  공식 Arq 7 스펙 + arq_restore 소스(BSD 3-Clause)에 명시.
- **공개되지 않은 부분**: Arq 7 의 `treepacks/`/`blobpacks/` `.pack` 컨테이너
  헤더·인덱스 레이아웃, 그리고 청커(`chunkerVersion: 3` + `useBuzhash`)
  파라미터.
- **현재 백업을 작성하는 공개 프로젝트는 없음** (`tcsc/larq` 가 "eventually
  writer" 목표를 명시했지만 5년 이상 비활성). 모든 서드파티 도구는
  read/restore 전용.
- **현실적 첫 단계**: 청커·pack 컨테이너를 우회하고 모든 객체를 단일
  EncryptedObject 로 `standardobjects/<shard>/<blobid>` 에 쓰는 v0.
  본 검증기로 round-trip 검증 가능, `arq_restore` (BSD) 로 Arq.app 호환성
  확인 가능.

### 9.4 Hetzner 특화 안전장치

reference 가 가진 connection-rate-limit 자동 감지(`Connection refused`,
`mux_client_request_session` 패턴 추적, 20회 연속 실패 시 조기 abort) 는
SftpBackend 에 아직 포팅되지 않았습니다. 운영자가 Hetzner 외 대상도
사용하는 경우 일반화된 형태로 추가 예정.

### 9.5 Arq 5/6 호환성

현재는 Arq 7 만 지원합니다. Arq 5/6 은 디렉토리 레이아웃이 다르고
(`bucketdata/<folder>/refs/heads/master` + packsets/), 본 프로젝트의
discover/검증 로직은 그대로 적용되지 않습니다.

## 10. 참고 자료

- Arq 7 데이터 포맷 (공식): <https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html>
- Arq 5 포맷 (재사용된 PBKDF2/HMAC 규칙 출처): <https://www.arqbackup.com/arq_data_format.txt>
- 본 검증기의 reference 구현: `neoocean/docker-monitor` →
  `scripts/arq-validate.py` (2,673 LOC)
- Reverse-engineering 보정값의 일차 출처: 운영자 환경 (Hetzner Storage
  Box) 에서의 실측 (`docker-monitor` SCENARIO §13, 2026-05-04 ~
  2026-05-05)
