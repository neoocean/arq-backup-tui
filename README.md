# arq-backup-tui

Arq Backup 7 포맷의 destination 을 다루는 독립 (independent) 검증기 +
복원기 + 작성기 + TUI. 순수 Python ≥ 3.9 + stdlib (HMAC, AES, LZ4 등은
모두 직접 구현하거나 시스템 `openssl` 만 호출).

## 1. 이 프로젝트의 의도와 그렇지 않은 의도

이 프로젝트는 [Arq Backup](https://www.arqbackup.com/) 사의 지적재산권을
침해할 의도가 **전혀 없습니다**. 만들어진 동기는 두 가지입니다:

1. **백업 파일포맷 설계의 학습**. 암호화·압축·dedup·content-addressable
   storage·incremental snapshot 같은 개념이 실제로 어떻게 결합되는지
   소스 레벨에서 이해하는 것이 본 저장소의 핵심 목적입니다. 한 줄로:
   "널리 사용되는 백업 도구가 내부적으로 어떻게 데이터를 배치하는가" 에
   대한 논리적 이해를 코드로 정리합니다.
2. **15년 넘게 사용해 온 Arq 백업의 장기 신뢰성 확보**. Arq.app 자체를
   대체하는 게 아니라, 운영자가 보유한 Arq 7 destination 의 무결성을
   Arq.app GUI 의 월간 자체 검증과 무관하게 **언제든 자기 손으로 검증**
   할 수 있고, GUI 가 사라지거나 호환되지 않는 환경에서도 데이터를
   **읽을 수 있도록** 보존하기 위함입니다. 향후에도 이 데이터를 신뢰하며
   사용하기 위한 second-source 도구입니다.

따라서 이 코드는 Arq Backup 의 **상업적 가치를 대체하는 도구가 아니며**,
다음 항목은 **의도적으로 구현하지 않습니다**:

- **S3 호환 스토리지 (S3, Wasabi, B2, Storj, GCS, Azure Blob, …) 지원**.
  Arq Backup 의 핵심 가치 제안 중 하나가 클라우드 백엔드 일괄 관리이며,
  본 프로젝트가 이 기능을 제공하면 Arq.app 의 라이선스 구매 동기를
  희석시킵니다. **클라우드 백엔드를 원하시면 [Arq Backup 라이선스를 구입](https://www.arqbackup.com/)** 하시기 바랍니다.
  (참고로 본 프로젝트는 로컬 / NAS / SFTP 만 지원하며, 클라우드 destination 이
  필요한 사용자는 `rclone mount` 우회로 사용해야 합니다.)
- **Arq.app GUI 의 운영 기능**: 스케줄 / 알림 / 메뉴바 / 시스템 트레이 /
  대시보드 / 클라우드 비밀번호 복구 / 라이선스 관리 등은 Arq.app 의
  policy layer 에 속하며 본 프로젝트의 범위 밖입니다.
- **`encryptedkeyset.dat` 파일 자체의 brute-force 도구**: 본 프로젝트의
  암호화 / 복호화 코드는 합법적인 비밀번호 보유자가 자기 자신의 백업을
  검증·복원하기 위함입니다. 비밀번호를 모르는 destination 에 대한 공격
  도구로 사용하지 마십시오.

## 2. Arq.app / arq_restore 와의 관계

본 프로젝트는 Arq Backup 의 공개된 데이터 포맷 사양을 reference
material 로 사용합니다:

- **공식 Arq 7 데이터 포맷 문서**: https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html
- **공식 Arq 5 포맷 문서** (Arq 7 의 PBKDF2 / HMAC 규칙 출처):
  https://www.arqbackup.com/arq_data_format.txt
- **`arq_restore` (BSD 3-Clause)**: Arq 사가 공개한 reference 복원 구현체.
  본 프로젝트는 `arq_restore` 의 소스를 **format claim 의 검증 reference**
  로 활용했습니다 (예: `Arq7BlobReader.m::dataForBlobLoc:` 의 분기 로직).
  파일 단위 / 라인 단위 복사가 아닌 alphanumeric format 의 binary layout
  을 Python 으로 재구현하는 형태이며, BSD 라이선스에 따라 출처를 본 항목
  에서 명시합니다.

`arq_restore` 의 BSD 3-Clause 라이선스 사본은 Haystack Software Inc. 의
공식 GitHub 저장소에서 확인할 수 있습니다.

또한 reference 가 명시하지 않은 일부 항목 (예: backuprecord 가 binary plist
가 아닌 JSON 으로 emit 된다는 점, BlobLoc 바이너리 레이아웃의 `isLargePack`
필드, Tree v4 의 38바이트 trailing block) 은 운영자의 실제 destination
바이트를 직접 분석해 reverse engineer 했습니다 (`docs/REAL-DATA-DISCOVERIES.md`
참조).

## 3. 무엇을 할 수 있는가

본 패키지가 제공하는 4가지 라이브러리:

| 패키지 | 역할 | 진입점 |
|---|---|---|
| `arq_validator` | Arq 7 destination 의 4단계 무결성 검증 (L0/L1a/L1b/L2 + audit-drip) | `python -m arq_validator` |
| `arq_reader` | Arq 7 (+ 5/6) 백업 → 로컬 파일 복원 | `python -m arq_reader` |
| `arq_writer` | 새 Arq 7 백업 destination 작성 | `python -m arq_writer` |
| `arq_tui` | 위 셋을 하나의 Textual TUI 로 통합 | `python -m arq_tui` 또는 `./arq-tui.py` |

### 검증 (validator)

```sh
python -m arq_validator --root /Volumes/arqbackup1 --tier deep \
    --password "$ARQ_PW"
```

4단계 tier:
- **L0** (`dry-run`): 디렉토리 모양만 (computer-UUID, 4 객체 패밀리, backupfolders)
- **L1a** (`quick`): ARQO 매직바이트 표본 sweep (기본 5%)
- **L1b** (`deep`): keyset 복호화 + 백업폴더별 최신 backuprecord HMAC
- **L2** (`audit`): 모든 EncryptedObject HMAC (+ 재개식 audit-drip 모드)

### 복원 (reader)

```sh
python -m arq_reader restore /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --folder-uuid <FU> --dest /tmp/restored
```

특정 historical record / 특정 path / 특정 source folder 모두 지정 가능.
Arq 5/6/7 모두 read 지원.

### 작성 (writer)

```sh
arq-backup create ~/Documents \
    --dest /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --use-packs --chunker arq_v7_41 \
    --exclude-glob '*.log' --max-file-bytes 1073741824
```

생성된 destination 은 본 검증기와 본 reader 로 byte-identical 라운드
트립이 보증되며, Arq.app GUI 측 호환성은 우리 reader 가 구분 못 하는
운영자 manual verification 이 필요한 영역입니다.

### TUI

```sh
./arq-tui.py    # 또는 python -m arq_tui
```

위 세 가지를 하나의 화면에서 다룰 수 있는 Textual TUI. 백업 / 복원 /
검증 / 정찰 / 백업 set 브라우저 / 보존 정책 적용 / 비밀번호 회전 /
플랜 편집 / 콘솔 (slash-command) 등.

## 4. 의존성·실행 환경

- **런타임**: Python ≥ 3.9 + 시스템 `openssl` (PATH 또는
  `--openssl-path`)
- **SFTP 사용 시**: 시스템의 OpenSSH `ssh` / `sftp` 클라이언트
- **Python 서드파티**: 없음 (TUI 만 `textual` 옵션 의존)
- **OS**: macOS / Linux. Windows 는 OpenSSH/openssl 동작 차이로 미지원.

## 5. 라이선스

본 저장소 자체는 **MIT 라이선스** (`LICENSE`).

Arq Backup, "Arq", 그리고 관련 상표는 Haystack Software Inc. 의
재산입니다. 본 프로젝트는 그 어떤 형태로든 Haystack Software 의 후원이나
공식 지지를 받은 적이 없습니다.

## 6. 더 읽을거리

- `DESIGN.md` — 프로젝트 전체 설계
- `docs/MECHANISM.md` — 백업/복원/검증 작동 원리 상세
- `docs/COVERAGE.md` — Arq 7 기능 패리티 매트릭스
- `docs/COMPATIBILITY.md` — 25개 Arq 7 invariant 락인
- `docs/COMPAT-VERIFICATION.md` — 호환성 검증 전략 카탈로그
- `docs/COMPAT-SFTP-TESTING.md` — 운영자 자격증명 기반 통합 테스트
- `docs/REAL-DATA-DISCOVERIES.md` — 실제 destination 으로 발견·수정한
  호환성 항목들 (`isLargePack`, JSON backuprecord, Tree v4 등)
- `docs/PLAN-tui.md` — TUI 설계 + 화면 카탈로그
- `docs/APFS-SNAPSHOTS.md` — macOS APFS 스냅샷 통합
- `docs/UNICODE.md` — 다국어 / 이모지 / 긴 경로 처리 보증
- `docs/RESEARCH-backup-creation-feasibility.md` — 작성기 작성 전 타당성 연구
- `docs/RESEARCH-format-extensions.md` — pack containers / 청커 / Arq 5–6
  기능 RE 노트
