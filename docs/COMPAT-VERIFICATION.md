# Arq 7 호환성 검증 전략 (sandbox 제약 하)

본 프로젝트의 개발 sandbox는 macOS Arq.app을 직접 실행할 수
없습니다. 그럼에도 writer / reader / validator의 Arq 7 호환성을
검증해야 하므로, 이 문서는 **operator-paste 워크플로** 와 **양쪽
호환성을 입증할 수 있는 비대칭 도구**를 catalogue합니다.

전략들의 공통 구조: **operator가 macOS 위에서 Arq.app을 한 번만
실행**한 결과를 sandbox로 paste / upload하면, sandbox 안의 자동화된
도구가 우리 라이브러리 출력과 비교하여 byte 레벨 차이를 보고합니다.

기존에 이 패턴으로 성공한 사례:
- **PR #1**: operator가 `Arq.app/Contents/MacOS/Arq` Mach-O
  binary를 분석한 JSON을 paste → sandbox가 T-table + chunker
  파라미터를 RE → `arq_writer.arq_chunker_params`에 land.

## 1. 전략 카탈로그

전략별 우선순위:

| 우선 | 전략 | 노력 | 가치 |
|:----:|------|:----:|:----:|
| ⭐ | A. **Shape fingerprint diff** | 작음 | 매우 높음 — 형식 / 청커 모든 mismatch 한 번에 감지 |
| ⭐ | B. **Cross-restore 검증** (Arq.app produced → 우리 reader) | 중간 | 매우 높음 — reader 호환성 직접 입증 |
| ⭐ | C. **Cross-restore 반대방향** (우리 writer → arq_restore CLI) | 중간 | 매우 높음 — writer 호환성 직접 입증 |
| ✓ | D. **Chunker oracle** (이미 구현됨) | 작음 | 높음 — 청커 파라미터 byte-level 검증 |
| ✓ | E. **Mach-O binary RE** (이미 PR #1로 land) | 작음 | 작음 — 청커 파라미터 한정 |
| ▲ | F. **Real backuprecord plist 수집** | 작음 | 중간 — `version`/`isComplete` 등 정확값 검증 |
| ▲ | G. **JSON sidecar value 비교** | 작음 | 작음 — Arq.app이 emit하는 정확값 capture |

본 PR이 제공하는 도구 + 문서로 ⭐ 표시 3개를 모두 가능하게 합니다.

---

## 2. ⭐ 전략 A — Shape fingerprint diff

### 2.1 무엇인가

**Salt-independent shape fingerprint**: 두 destination이 같은
source tree에서 만들어졌다면 동일해야 하는 구조적 요약. 다음을
포함:

- 디렉터리 layout (computer / folder / record / pack 카운트)
- JSON sidecar의 **schema** (key 이름 + 값 타입; 값 자체는 제외)
- 각 backuprecord의 plist key 목록 + 메타 (`version`,
  `isComplete`, `computerOSType`, `creationDate`)
- 각 record 내부의 모든 파일에 대해
  `(rel_path, item_size, mtime_sec, mode_perms, is_symlink,
  chunk_sizes=[len_1, len_2, ...])`

`blob_id` / `encryption_key` / `keyset_salt` 처럼 키셋마다 다른
값은 **의도적으로 제외**하므로, Arq.app destination과 우리 writer
destination을 그대로 비교 가능합니다.

### 2.2 도구

- 모듈: `arq_validator.fingerprint`
- API: `compute_shape_fingerprint(backend, *, encryption_password)
  → dict`, `diff_fingerprints(a, b) → dict`
- CLI: `arq-fingerprint compute <path> --password ...`,
  `arq-fingerprint compare <a.json> <b.json>`

### 2.3 Operator 워크플로

#### A.1 Source 준비 (reproducible)

operator가 macOS 위에서:

```bash
# 1) 알려진 fixture를 만든다 (reproducible)
mkdir -p /tmp/compat-src && cd /tmp/compat-src
echo "alpha" > alpha.txt
echo "beta" > beta.txt
mkdir -p subdir
echo "gamma" > subdir/gamma.txt
mkdir -p 한글
echo "내용" > 한글/메모.txt

# 2) 모든 파일의 mtime을 고정 (sub-second resolution 차이 회피)
find . -exec touch -h -t 202601011200.00 {} \;
```

#### A.2 Arq.app으로 백업

operator가 macOS Arq.app GUI에서:

1. 새 plan 생성 → source = `/tmp/compat-src`
2. destination = local folder, 예: `/Volumes/External/arq-arqapp`
3. password = `compat-test-pw`
4. 즉시 한 번 실행

#### A.3 Fingerprint 추출 (macOS)

operator가 macOS에서 (이 라이브러리 설치 후):

```bash
arq-fingerprint compute /Volumes/External/arq-arqapp \
    --password compat-test-pw \
    --out /tmp/fingerprint-arqapp.json
```

이 JSON 파일을 sandbox로 paste하거나 업로드.

#### A.4 우리 writer로 같은 source를 백업 + fingerprint (sandbox)

sandbox 안에서:

```bash
arq-backup create /tmp/compat-src \
    --dest /tmp/arq-ours --password compat-test-pw \
    --use-packs

# Arq.app 청커 파라미터 매칭 모드로:
python3 -c "import arq_writer.arq_chunker_params; \
            from arq_writer import build_backup; \
            build_backup('/tmp/compat-src', '/tmp/arq-ours', 'compat-test-pw', \
                         use_packs=True, \
                         chunker_config=arq_writer.arq_chunker_params.ARQ_V7_CHUNKER_CONFIG)"

arq-fingerprint compute /tmp/arq-ours \
    --password compat-test-pw \
    --out /tmp/fingerprint-ours.json
```

#### A.5 Diff

```bash
arq-fingerprint compare \
    /tmp/fingerprint-arqapp.json \
    /tmp/fingerprint-ours.json
```

출력의 `summary` 섹션이 모두 0이고 `match: true`면 **byte-level
구조 호환** 입증. 어느 카테고리가 0이 아니면:

- `sidecar_schema_diffs` — JSON config의 key/type 차이 → 우리
  writer의 sidecar 스키마 미스매치 (예: 특정 key 누락)
- `chunk_pattern_diffs` — 청커 파라미터 미스매치 → 어느 파일이
  몇 개 청크로 어떤 크기로 갈라지는지 정확히 표시 → 청커 RE
  업데이트의 입력
- `file_shape_diffs` — mode / size mismatch
- `missing_files_in_a` / `missing_files_in_b` — 한쪽이 빠뜨린
  파일

### 2.6 자동화된 회귀 테스트 (sandbox에서 실행)

`tests/test_fingerprint.py` 6개 테스트:

- 같은 source → 같은 fingerprint
- 청커 다르면 chunk_pattern_diffs 발생
- 파일 누락은 missing_files_* 에 등록
- Unicode 경로명이 fingerprint에 verbatim
- diff `match` 필드가 동일 fingerprint에 True

이 테스트들은 우리 writer + reader가 자기 자신과 호환됨을 보장
합니다. **Arq.app 호환성**은 §A.1–A.5 절차의 operator-paste 결과로
입증됩니다.

---

## 3. ⭐ 전략 B — Cross-restore (Arq.app → 우리 reader)

### 3.1 무엇인가

Arq.app이 만든 destination을 우리 reader로 복원하여 source와 byte
identical 한지 검증. 우리 reader의 **읽기 호환성 입증**.

### 3.2 Operator 워크플로

#### B.1 Source 준비
A.1과 동일.

#### B.2 Arq.app 백업
A.2와 동일.

#### B.3 Destination tarball 생성 (macOS)

```bash
cd /Volumes/External
tar czf /tmp/arq-arqapp.tgz arq-arqapp
shasum -a 256 /tmp/arq-arqapp.tgz
```

이 tarball + SHA-256을 sandbox로 업로드.

#### B.4 Sandbox에서 cross-restore 검증

```bash
mkdir -p /tmp/cross-restore
tar xzf /tmp/arq-arqapp.tgz -C /tmp/cross-restore

arq-reader restore \
    --src /tmp/cross-restore/arq-arqapp \
    --password compat-test-pw \
    --dest /tmp/restored

# 원본과 비교
diff -r /tmp/compat-src /tmp/restored && echo "BYTE-IDENTICAL"
```

`BYTE-IDENTICAL`이 출력되면 우리 reader가 Arq.app 결과물을 정확히
복원할 수 있음 — **읽기 호환성 입증**.

차이가 있다면 `diff -r`이 어떤 파일이 어디서 다른지 알려주므로
reader의 어느 코드 경로가 잘못되었는지 추적 가능.

### 3.3 자동화 후크

operator가 tarball을 paste한 시점부터는 sandbox 안에서 자동화
가능. `tests/integration/test_arqapp_cross_restore.py` 같은
파일을 만들어 `tests/fixtures/arqapp_destinations/*.tgz`에서
모든 fixture를 cross-restore하면 회귀 보호 완성.

---

## 4. ⭐ 전략 C — Cross-restore (우리 writer → arq_restore)

### 4.1 무엇인가

우리 writer가 만든 destination을 **공식 BSD `arq_restore` CLI** 로
복원하여 우리 source와 byte identical 한지 검증. **쓰기
호환성 입증**.

`arq_restore`는 BSD 3-Clause 라이선스로 https://github.com/arq-backup/
arq_restore에서 빌드 가능. macOS / Linux 모두에서 빌드됩니다.

### 4.2 Operator 워크플로

#### C.1 우리 writer로 백업 (sandbox)
이전 A.4와 동일.

#### C.2 destination tarball 생성 (sandbox)

```bash
cd /tmp && tar czf /tmp/arq-ours.tgz arq-ours
shasum -a 256 /tmp/arq-ours.tgz
```

operator가 이 tarball을 macOS / Linux로 가져감.

#### C.3 arq_restore 빌드 + 실행 (operator 머신)

```bash
git clone https://github.com/arq-backup/arq_restore
cd arq_restore && make
mkdir -p /tmp/restore-arq_restore
arq_restore /path/to/arq-ours/<COMPUTER-UUID> \
    --password compat-test-pw \
    --output /tmp/restore-arq_restore
diff -r /tmp/compat-src /tmp/restore-arq_restore
```

#### C.4 결과 paste

`arq_restore`의 종료 코드 + `diff -r` 출력을 sandbox로 paste.
종료 코드 0 + diff 빈 출력 = **쓰기 호환성 입증**.

---

## 5. ✓ 전략 D — Chunker oracle (이미 구현)

### 5.1 무엇인가

**파일 한 개**의 청크 길이 sequence를 우리 writer와 Arq.app
사이에서 정확히 비교하는 도구. PR #1에서 이미 land:

- 모듈: `arq_writer.chunker_oracle`
- CLI: `arq-buzhash-find verify-chunking <input> <observed-lengths.json>`

전략 A의 fingerprint diff가 이미 chunk_sizes를 포함하므로 사실상
이 oracle을 흡수합니다. 하지만 **단일 파일 디버깅** 용도로는 더
간결하므로 유지.

### 5.2 Operator 워크플로

PR #1의 `docs/RESEARCH-format-extensions.md` §4.2에 절차 기록되어
있음. 핵심:

1. operator가 macOS에서 알려진 input.bin을 Arq.app으로 백업
2. 결과 backuprecord에서 그 파일의 `dataBlobLocs[*]` 의 plaintext
   length 시퀀스를 추출 (`arq_restore`로 backuprecord 복호)
3. JSON 배열로 paste
4. sandbox에서 `arq-buzhash-find verify-chunking input.bin
   observed-lengths.json` → 우리 chunker 출력과 byte-level 비교

---

## 6. ▲ 전략 F — Real backuprecord plist 수집

### 6.1 무엇인가

operator가 Arq.app destination에서 한 backuprecord의 복호된 plist
dump를 paste하면, 우리 fingerprint / writer가 emit하는 plist와
key-by-key 비교 가능. 정확값 (예: `arqVersion`이 정확히 어떤
문자열인지, `version` 정수가 100인지 200인지) 을 capture할 수 있는
유일한 방법.

### 6.2 Operator 워크플로

```bash
# macOS에서:
arq_restore --dump-record /path/to/.backuprecord \
    --password compat-test-pw > /tmp/record.plist
plutil -convert xml1 -o - /tmp/record.plist > /tmp/record.xml
# 또는 binary 그대로 paste:
shasum -a 256 /tmp/record.plist
xxd /tmp/record.plist | head -100   # paste-friendly hex dump
```

operator가 결과를 paste하면 sandbox에서 `plistlib.loads`로
parsing해서 우리 writer가 emit하는 record와 key/type 비교.

이 정보로 우리 writer의 `arq_writer.backuprecord:build_backuprecord_dict`를
미세조정 가능 (key 추가 / 정확한 default 값 확립 등).

---

## 7. ▲ 전략 G — JSON sidecar value 비교

### 7.1 무엇인가

`backupplan.json` / `backupconfig.json` 등 sidecar에 들어가는
정확한 default 값을 Arq.app이 어떻게 채우는지 capture. 우리는
이미 이 값들을 spec + 추정으로 emit하지만, 일부 field (예:
`maxPackedItemLength` 정확값, `cpuUsage` default, `scheduleJSON`
구조)는 실제값을 봐야 정확히 매칭됩니다.

### 7.2 Operator 워크플로

```bash
cat /Volumes/External/arq-arqapp/<COMPUTER-UUID>/backupplan.json
cat /Volumes/External/arq-arqapp/<COMPUTER-UUID>/backupconfig.json
```

paste → sandbox에서 `tests/fixtures/arqapp_sidecars/*.json` 으로
보존 → `arq_writer.json_configs`의 default가 차이 없도록 조정.

---

## 8. 종합: 호환성 검증 매트릭스

| 검증 대상 | 전략 |
|----------|------|
| **Reader가 Arq.app 출력을 읽는다** | B (cross-restore) |
| **Writer 출력이 Arq.app 호환** | C (arq_restore로 cross-restore) + A (fingerprint diff) |
| **청커 파라미터 일치** | A (chunk_pattern_diffs 0건) + D (oracle) + E (Mach-O RE; 이미 land) |
| **JSON sidecar key 누락** | A (sidecar_schema_diffs 0건) + G (값 비교) |
| **backuprecord plist key 누락** | A (node_schema 비교) + F (실측 plist) |
| **파일 metadata 보존 (mode / mtime)** | A (file_shape_diffs 0건) + B / C (실제 복원 비교) |

### 8.1 운영자 체크리스트 (한 번 실행)

호환성을 한 번 입증하려면:

1. ☐ A.1–A.5 (fingerprint diff) — Arq.app + 우리 writer 양쪽 결과 비교
2. ☐ B.1–B.4 (cross-restore) — Arq.app destination을 우리 reader로 복원
3. ☐ C.1–C.4 (arq_restore reverse) — 우리 writer destination을 arq_restore로 복원

각 체크가 통과하면 **Arq 7 호환성이 byte-level로 입증**됩니다.
실패한 항목은 fingerprint diff 또는 `diff -r` 출력으로 정확한
mismatch 위치를 식별 가능.

---

## 9. 본 PR이 추가한 자동화 도구 요약

| 항목 | 위치 |
|------|------|
| Shape fingerprint 모듈 | `arq_validator/fingerprint.py` |
| `compute_shape_fingerprint(backend, *, encryption_password) → dict` | API |
| `diff_fingerprints(a, b) → dict` | API |
| `arq-fingerprint compute <path>` CLI | `arq_validator/fingerprint_cli.py` |
| `arq-fingerprint compare <a.json> <b.json>` CLI | 같은 모듈 |
| 회귀 테스트 (6개) | `tests/test_fingerprint.py` |

다음 단계 (별도 PR):

- 전략 B / C 의 자동화 fixture (operator-paste 산출물을
  `tests/fixtures/arqapp_destinations/` 트리에 보존하면 sandbox
  에서 자동 회귀)
- 전략 F / G 의 paste-결과 보관 + 자동 비교
