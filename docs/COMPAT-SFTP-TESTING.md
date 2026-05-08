# 실제 SFTP destination 기반 호환성 테스트

> **Status (2026-05-08)**: ✅ PR #9 에서 형식/형상 호환성 검증
> harness (`test_arqapp_sftp_compat.py`) 가 도입됨. 후속으로
> `test_arq_real_destination.py` 와 `.secrets/` 자격증명 디렉터리가
> 추가되어 **reader / validator / writer 세 기둥의 런타임 동작**을
> 실 destination 에서 검증할 수 있게 되었습니다. 모든 통합 테스트는
> 기본 환경에서는 skip 되고, 자격증명을 제공한 운영자에게만
> 실행됩니다.

본 문서는 운영자가 **실제 운영 중인 Arq 7 SFTP destination**을
sandbox에서 사용하여 reader / validator / writer / fingerprint
호환성을 자동 검증하는 절차를 정의합니다. `docs/COMPAT-VERIFICATION.md`
에서 ⭐로 표시한 Strategy A + B를 자동화된 회귀 테스트로 전환
합니다.

## 0. 자격증명 소스 — `.secrets/` 또는 `.env`

자격증명은 다음 세 소스 순서로 해결됩니다 (먼저 발견된 값이 우선):

1. **`.secrets/`** (권장) — 워크스테이션에서 장기간 유지하는 경우
   추천. 한 곳에 모여 있어 감사 / 회전이 쉽습니다. 레이아웃:
   ```
   .secrets/
   ├── README.md                  ← 커밋됨 (안내)
   ├── sftp.json.example          ← 커밋됨 (템플릿)
   ├── dest_password.example      ← 커밋됨 (템플릿)
   ├── sftp.json                  ← 로컬 전용, 실제 SFTP 정보
   └── dest_password              ← 로컬 전용, Arq 암호화 비밀번호
   ```
   설정 절차:
   ```sh
   cp .secrets/sftp.json.example      .secrets/sftp.json
   cp .secrets/dest_password.example  .secrets/dest_password
   $EDITOR .secrets/sftp.json
   $EDITOR .secrets/dest_password
   chmod 600 .secrets/sftp.json .secrets/dest_password
   ```
2. **`.env`** (legacy) — `KEY=VALUE` 한 줄씩, 한 파일. PR #9
   호환을 위해 유지.
3. **`os.environ`** (CI / 일회성) — 직접 환경변수 export.

`.gitignore` / `.p4ignore` 가 실 자격증명 파일 (`.secrets/sftp.json`,
`.secrets/dest_password`, `.env`, `.env.local`) 을 git / Perforce 양쪽에서
모두 제외합니다. 템플릿 (`README.md`, `*.example`) 만 커밋됩니다.

## 1. 보안 정책

### 1.1 자격증명 처리 원칙

- **Git에 절대 커밋 안 함**: `.env` / `.env.local` / `tests/
  integration/.env`이 모두 `.gitignore`에 등록됨
- **로컬 파일만**: `.env` 파일은 운영자 머신에 머무름. CI / GitHub /
  remote에 노출되지 않음
- **읽기 전용**: 모든 integration 테스트는 destination에 **쓰기
  연산을 수행하지 않음** (SftpBackend의 read_all / read_range /
  list_dir만 호출)
- **로그 격리**: SSH 비밀번호 / Arq 비밀번호는 stdout / stderr /
  실패 메시지에 절대 노출되지 않도록 코드 작성

### 1.2 PII 노출 방지

테스트는 **파일 컨텐츠의 정확한 값**을 절대 assert하지 않습니다.
- 구조 검증만: 파일 존재 여부, 사이즈 범위, ARQO magic, HMAC
  match, blob_id 자기일관성
- 운영자의 실제 파일 내용은 메모리에서만 처리되고 디스크 / 로그
  로 떨어지지 않음
- 샘플 복원도 임시 디렉터리 (`tempfile.TemporaryDirectory`)에
  쓰고 즉시 cleanup

### 1.3 권장 자격증명 운영

- 가능하면 **read-only 전용 SFTP 계정** 사용
- `chrooted` 또는 `ChrootDirectory` 설정으로 destination
  디렉터리만 노출
- 비밀번호 대신 **SSH key**로 인증 (회수 / 회전이 용이)
- Arq 비밀번호는 destination별로 다른 값을 사용 (재사용 금지)

## 2. 환경 변수 contract

| 변수 | 필수 | 설명 |
|------|:----:|------|
| `ARQ_TEST_SFTP_HOST` | ✓ | SFTP 호스트명 또는 IP |
| `ARQ_TEST_SFTP_USER` | ✓ | SSH 사용자명 |
| `ARQ_TEST_SFTP_PORT` | | 기본 22 |
| `ARQ_TEST_SFTP_ROOT` | ✓ | 서버측 destination root 경로 (예: `/home/u123/arq`). `<COMPUTER-UUID>/` 디렉터리들이 그 아래에 있어야 함 |
| `ARQ_TEST_SFTP_AUTH_PASSWORD` | △ | SSH 비밀번호 |
| `ARQ_TEST_SFTP_IDENTITY` | △ | SSH 개인키 파일 경로 |
| `ARQ_TEST_DEST_PASSWORD` | ✓ | Arq destination의 암호화 비밀번호 (SSH 비밀번호와 별개) |

`ARQ_TEST_SFTP_AUTH_PASSWORD`와 `ARQ_TEST_SFTP_IDENTITY` 중 **최소
하나**는 설정. 둘 다 비어 있으면 테스트가 자동 스킵.

## 3. 설정 절차

### 3.1 .env 파일 작성

```bash
cd /path/to/arq-backup-tui
cp .env.example .env
chmod 600 .env       # 다른 사용자가 읽지 못하게
# 에디터로 .env를 열어 실제 값 채우기
```

### 3.2 sanity check

```bash
# 자격증명이 인식되는지 확인 (CI에서는 자동 skip)
python -m unittest discover -s tests/integration -v
```

자격증명이 없으면 모든 테스트가 다음 메시지로 skip됨:

```
real-SFTP integration tests skipped — no credentials in env
(see docs/COMPAT-SFTP-TESTING.md)
```

자격증명이 있으면 SSH master 한 번 setup → 7개 테스트 실행.

### 3.3 운영자 paste 워크플로 (chat 인터페이스)

자격증명이 sandbox에 없으므로, 운영자가 **로컬에서 .env를 작성한
뒤 통합 테스트 결과를 paste**하는 방식이 됩니다:

```bash
# 운영자 머신에서:
git pull origin main
cp .env.example .env && chmod 600 .env
# .env 편집 (자격증명 입력)
python -m unittest discover -s tests/integration -v 2>&1 | tail -50
```

이 출력 (전체 또는 마지막 50줄)을 chat에 paste하시면, sandbox에서
실패 원인을 분석하고 fix를 land할 수 있습니다.

**중요**: paste 시 출력에 SSH 비밀번호 / Arq 비밀번호가 포함되지
않도록 **테스트 자체가 자격증명을 절대 출력하지 않게** 작성되어
있습니다 (`tests/integration/_creds.py`). 그래도 paste 전에 한 번
검토 권장.

## 4. 테스트 카탈로그

### 4.1 형식·형상 호환성 — `test_arqapp_sftp_compat.py` (PR #9, 7 tests)

| 테스트 | 무엇을 검증 |
|--------|------------|
| `test_layout_discovers_computer` | `discover_layout`이 적어도 한 컴퓨터 + 폴더 발견 |
| `test_keyset_decrypts` | `encryptedkeyset.dat` PBKDF2-SHA256 + AES-CBC + HMAC 복호 + 32B field 모양 |
| `test_compatibility_audit_passes` | `check_arq7_compatibility`의 25 invariant 모두 통과 |
| `test_validator_l0_l1a_l1b_tiers_pass` | QUICK + DEEP tier 통과 (L2 audit는 너무 길어 별도 실행 필요) |
| `test_fingerprint_is_well_formed_json` | `compute_shape_fingerprint` 출력이 JSON-serialize 가능, schema_version=1 |
| `test_records_list_at_least_one` | 최소 한 개 backuprecord 존재 |
| `test_sample_standalone_object_arqo_valid` | 1 MiB 이하 standalone object 16개 sampling → ARQO + HMAC + blob_id = SHA-256(salt+plaintext) 검증 |

### 4.2 런타임 동작 — `test_arq_real_destination.py` (3 tests)

세 기둥 (reader / validator / writer) 의 실제 동작을 sandbox 가
아닌 운영자의 실 destination 에서 검증합니다. 단, **writer 는 절대
운영자 destination 의 root 에 쓰지 않고** `creds.write_subdir`
(기본 `.arq-backup-tui-write-test`) 의 dot-prefixed 서브디렉터리에서
만 동작합니다.

| 테스트 | 클래스 | 무엇을 검증 |
|--------|--------|------------|
| `test_restore_latest_record_of_first_folder` | `RealDestinationReaderTests` | 첫 폴더의 최신 record 를 tempdir 에 복원 → 트리에 비-empty 파일 존재 (decrypt 가 정상이면 반드시 통과) |
| `test_audit_drip_capped_at_a_few_megabytes` | `RealDestinationValidatorTests` | L2 audit-drip 4 MiB / 20 초 cap 으로 실행 → 실패 0, cursor 진행 |
| `test_round_trip_via_real_sftp` | `RealDestinationWriterTests` | sandbox 디렉터리에 합성 backup 작성 → reader 로 복원 → byte-identical 비교 (alpha.txt + 한글.txt + subdir/gamma.bin) → DEEP tier validator |

writer 테스트는 setUp / tearDown 에서 sandbox 를 `rm -rf` 로
재초기화 / 정리합니다. 운영자의 실 데이터는 절대 변경되지
않습니다.

각 테스트는 **자체적으로 SFTP 마스터 1개**를 setup하고 cleanup하므로
순서 독립.

## 5. 자동화 후크 (CI에서 안 돌아감 — local only)

기본 `python -m unittest discover`는 `tests/integration/`을
포함하지만, 자격증명이 없으면 모두 auto-skip되므로 CI는 영향
없습니다. 운영자 머신에서만 자격증명이 채워져 실제 검증.

CI에 SFTP 자격증명을 secret으로 등록할 의향이 있다면 별도 워크플로
파일 (`.github/workflows/sftp-integration.yml`) 추가 가능 — secret
값들이 외부 PR에 leak되지 않도록 `pull_request_target` 보안 정책
필수.

## 6. 발견 사례 + fix 흐름

운영자가 paste한 결과에서 어느 invariant가 실패했는지에 따라
다음 fix 패턴 적용:

| 실패 invariant | 원인 / 해결 |
|---------------|------------|
| `L1` (top-level UUID 없음) | 잘못된 root 경로; `.env` 의 `ARQ_TEST_SFTP_ROOT` 수정 |
| `C1` (keyset magic 불일치) | 파일 손상; SFTP server filesystem 검증 |
| `C3` (keyset 복호 실패) | Arq 비밀번호가 틀림 (`ARQ_TEST_DEST_PASSWORD`) |
| `L3` (config 키 누락) | Arq.app 신버전이 새 키 추가 → 우리 reader가 모름; 우리 `_BACKUPCONFIG_REQUIRED`에 추가 |
| `L4` (plan 키 누락 / 타입 mismatch) | 동일; `_BACKUPPLAN_REQUIRED` 또는 `_BACKUPFOLDER_REQUIRED` 업데이트 |
| `B2` (backuprecord 키 누락) | 우리 `_BACKUPRECORD_REQUIRED_KEYS` 업데이트 |
| `A1` (ARQO magic 불일치) | 파일 손상 또는 우리가 모르는 새 envelope 포맷; spec 재확인 |
| `ID2` (blob_id mismatch) | 파일 손상 또는 우리 `compute_blob_id` 알고리즘 변경 필요 |
| `validator l1a / l1b 실패` | 동일 원인; tier 결과 출력에서 정확한 파일 경로 확인 가능 |

## 7. SFTP 자격증명 chat-paste 가이드

운영자가 **sandbox에 직접** SFTP 접근을 주려면 (chat 인터페이스
한정으로):

옵션 A — `.env` 내용을 chat으로 paste:

```
ARQ_TEST_SFTP_HOST=...
ARQ_TEST_SFTP_USER=...
ARQ_TEST_SFTP_PORT=22
ARQ_TEST_SFTP_ROOT=/...
ARQ_TEST_SFTP_AUTH_PASSWORD=... 또는 ARQ_TEST_SFTP_IDENTITY=~/...
ARQ_TEST_DEST_PASSWORD=...
```

→ sandbox 측에서 위 값을 환경변수로 export하고 `python -m unittest
discover -s tests/integration -v` 실행 → 결과 paste back.

**chat 메시지에 비밀번호가 포함되므로 보안에 매우 주의**:
- 테스트 직후 비밀번호 회전 권장
- 가급적 read-only 전용 SSH 계정 사용
- 일회용 자격증명으로 한정

옵션 B — SSH key paste:

운영자가 **개인키 내용**을 chat에 paste:

```
-----BEGIN OPENSSH PRIVATE KEY-----
...키 내용...
-----END OPENSSH PRIVATE KEY-----
```

→ sandbox에서 `/tmp/test-key`로 저장 (mode 0600) →
`ARQ_TEST_SFTP_IDENTITY=/tmp/test-key`로 export → 테스트 실행 →
끝나면 키 파일 즉시 삭제.

옵션 B가 비밀번호보다 안전 (회수 / 회전이 용이; CSV / DB log에
실수로 들어가도 키 파일이 없으면 사용 불가).

옵션 C — 운영자 머신에서 실행 + 결과만 paste:

가장 안전. 운영자가 로컬에서 자신의 `.env`로 테스트 실행 →
결과 텍스트만 chat에 paste → sandbox는 자격증명 없이도 분석 +
fix 가능.

권장: 가능하면 옵션 C 사용. 부득이 옵션 A / B 사용 시 일회용
자격증명 + 사용 후 즉시 회전.

## 8. 향후 작업

- 운영자가 fixture를 paste하면 `tests/fixtures/arqapp_real_sftp/`
  로 보존 → CI에서 매 PR마다 자동 회귀
- `tests/integration/test_arqapp_local_compat.py` 동일 구조의 로컬
  destination 버전 (SFTP 의존 없이도 회귀 가능)
- AUDIT (L2) tier도 별도 실행 가능하도록 환경변수
  `ARQ_TEST_RUN_FULL_AUDIT=1` 추가
