# 동작 원리: 백업 생성 / 검증 / 복원

이 문서는 현재 `arq-backup-tui` 코드베이스가 **무엇을 어떻게**
하는지를 단계별로 추적합니다. 모든 단락은 실제 모듈/함수/줄
참조를 포함하므로, 각 단계에 대응하는 소스 코드를 곧바로
열어 확인할 수 있습니다.

세 가지 핵심 흐름:

1. **백업 생성**: `arq_writer.Backup.add_folder` (또는 한 번에
   처리하는 `arq_writer.build_backup`)
2. **검증**: `arq_validator.validate` (4 tier) + `audit_drip` +
   `arq_validator.check_arq7_compatibility` (형식 conformance)
3. **복원**: `arq_reader.Restore.restore`

각 흐름의 입력 / 부산물 / 출력 / 호출 그래프를 차례로 기술합니다.

---

## 0. 공통 개념

세 흐름이 모두 의지하는 핵심 개념과 모듈을 먼저 정리합니다.

### 0.1 디렉터리 레이아웃 (Arq 7)

```
<dest_root>/
└── <COMPUTER-UUID>/                        # 8-4-4-4-12 hex (대문자)
    ├── encryptedkeyset.dat                 # 마스터 키 (암호화)
    ├── backupconfig.json                   # 컴퓨터 단위 설정
    ├── backupplan.json                     # 백업 플랜 스냅샷
    ├── backupfolders.json                  # 폴더 인덱스
    ├── standardobjects/<2-hex>/<62-hex>    # 단일 blob 저장 (옵션)
    ├── treepacks/<2-hex>/<UUID>.pack       # 트리 blob 묶음 (옵션)
    ├── blobpacks/<2-hex>/<UUID>.pack       # 데이터 blob 묶음 (옵션)
    ├── largeblobpacks/<2-hex>/<UUID>.pack  # 대용량 blob 묶음 (읽기만)
    └── backupfolders/<FOLDER-UUID>/
        ├── backupfolder.json
        └── backuprecords/<5자리 bucket>/<num>.backuprecord
```

`bucket` = `floor(creation_date / 100000)` (5자리 영점 채움), `num`
= `creation_date % 100000`. 두 값이 합쳐져서 chronological한
record 순서를 형성합니다.

### 0.2 핵심 byte-level 형식

#### EncryptedObject (`ARQO`) — 모든 blob의 외피

```
0..4    "ARQO" (magic)
4..36   HMAC-SHA256(hmac_key, body[36:end])
36..52  master_iv (16B)
52..116 AES-256-CBC( encryption_key, master_iv,
                     data_iv (16B) ‖ session_key (32B) )
                                                   = 64B (PKCS7 패딩 포함)
116..end AES-256-CBC( session_key, data_iv, plaintext )
```

`encryption_key` / `hmac_key`는 keyset의 마스터 키들. `session_key`
와 `data_iv`는 각 blob 단위로 새로 생성됨.

#### LZ4 wrap

ARQO 본문에 들어가기 전, plaintext는 LZ4 block 형식으로 감싸집니다:

```
0..4    big-endian uint32 = decompressed_length
4..end  LZ4 block (raw, no frame)
```

decompress 시에는 length로 미리 출력 버퍼를 잡고 LZ4 block을
풀어 길이를 검증합니다.

#### encryptedkeyset.dat — 마스터 키 자체를 암호화

```
0..25   "ARQ_ENCRYPTED_MASTER_KEYS"  (25B literal, NUL pad 없음)
25..33  PBKDF2 salt (8B)
33..65  HMAC-SHA256( derived[32:64], iv ‖ ciphertext ) (32B)
65..81  AES-256-CBC IV (16B)
81..end AES-256-CBC( derived[0:32], iv,  plaintext )
```

여기서 `derived` = `PBKDF2-HMAC-SHA256(password, salt,
iterations=200_000, dklen=64)`.

plaintext 레이아웃 (binary):

```
[uint32 BE: version=3]
[uint64 BE: 32] [encryption_key (32B)]
[uint64 BE: 32] [hmac_key       (32B)]
[uint64 BE: 32] [blob_id_salt   (32B)]
```

### 0.3 blob_id (content addressing)

```
blob_id = SHA-256( blob_id_salt ‖ plaintext ).hexdigest()  # 64자 lowercase hex
```

`plaintext`는 LZ4 wrap 전, 즉 raw 파일 청크 / 직렬화된 Tree /
backuprecord plist입니다. 동일 컨텐츠는 동일 blob_id를 가지므로
중복 제거가 자연스럽게 일어납니다.

### 0.4 Backend 추상

`arq_validator.backend.Backend` Protocol — 6개의 read 메서드와
2개의 write 메서드:

```python
list_dir / stat_size / read_range / read_all / exists / is_dir
mkdir / write_all
```

구현은 `LocalBackend` (로컬 파일시스템) 또는 `SftpBackend`
(OpenSSH master + SFTP). 모든 writer / reader / validator I/O는
backend를 통해서 흐릅니다.

---

## 1. 백업 생성 흐름

CLI 진입: `arq-backup` (`arq_writer.cli`) 또는 TUI의 `BackupRunScreen`.
프로그래밍 진입: `arq_writer.build_backup(...)` 한 줄 또는
`arq_writer.Backup` 클래스를 직접 사용.

### 1.1 `Backup.__init__` — 키 / 백엔드 / 캐시 초기화

`arq_writer/backup.py`의 `Backup` 클래스 초기화:

1. **백엔드 결정**:
   - `backend` 인자가 None이면 `dest_root`를 만들고
     `LocalBackend(dest_root)`로 감쌈
   - 인자가 주어지면 그대로 사용 (보통 SftpBackend)

2. **마스터 키 결정** (`dedup_against_existing` 분기):
   - `dedup_against_existing=False` 또는 첫 백업: 32B씩 무작위로
     `encryption_key` / `hmac_key` / `blob_id_salt` 생성
     (`secrets.token_bytes`)
   - `dedup_against_existing=True` + 키 인자가 모두 None:
     `_try_load_existing_keyset(dest_root, computer_uuid, password)`
     호출 → 기존 destination의 encryptedkeyset.dat을 복호화하고
     기존 키들을 재사용. 이렇게 해야 새 blob들의 SHA-256
     blob_id가 이전 실행의 것과 일치하여 dedup 캐시 히트가 가능.

3. **청커 초기화** (`chunker_config`이 주어진 경우):
   - `Buzhash(config)` 인스턴스 생성. T 테이블 + window 크기 +
     boundary mask를 보유.
   - `arq_writer.arq_chunker_params`를 import하면 Arq.app v7.41
     실측 파라미터 (256-byte window, 16-bit mask, 128 KiB max,
     reverse-engineered T table)가 자동 등록되어
     `chunker_for_arq(3, True)`로 가져올 수 있음.

4. **per-run 누적기 초기화**:
   - `_written_blobs: Dict[blob_id, BlobLoc]` — 실행 내 dedup 캐시
   - `files_written` / `files_reused` / `bytes_plaintext` 등의
     카운터
   - `_blob_pack` / `_tree_pack` — 패킹 모드일 때 lazy하게
     PackBuilder 생성

### 1.2 `init_plan()` — 메타 파일 작성 + dedup 시드

`Backup.init_plan` (`arq_writer/backup.py`):

1. **디렉터리 생성**:
   - `<cu>/standardobjects/`
   - `<cu>/backupfolders/`
   - 모두 backend.mkdir 경유 (LocalBackend 또는 SftpBackend가
     실제로 mkdir / `mkdir -p`를 실행)

2. **encryptedkeyset.dat 작성** (`_keyset_was_reused`가 False일 때만):
   - `crypto_write.build_encrypted_keyset(password, encryption_key,
     hmac_key, blob_id_salt)` 호출
   - 내부 단계 (`crypto_write.py`):
     a. plaintext 빌드: version=3 + 3 × (uint64 length + 32B key)
     b. 8B salt 생성 (`secrets.token_bytes(8)`)
     c. 16B IV 생성
     d. derived = `hashlib.pbkdf2_hmac("sha256", password, salt,
        200_000, dklen=64)`
     e. ciphertext = `aes_256_cbc_encrypt(derived[:32], iv,
        plaintext)` (호스트 `openssl` CLI 호출)
     f. mac = `HMAC-SHA256(derived[32:], iv ‖ ciphertext)`
     g. 최종: `KEYSET_MAGIC ‖ salt ‖ mac ‖ iv ‖ ciphertext`
   - backend.write_all로 디스크에 기록

3. **JSON sidecar 3종 작성**:
   - `backupconfig.json`: chunkerVersion=3, blobIdentifierType=2
     (SHA-256), maxPackedItemLength=256000, isEncrypted=True 등
   - `backupplan.json`: planUUID, version=2, scheduleJSON,
     transferRateJSON, emailReportJSON, backupFolderPlansByUUID
     (지금은 빈 dict; add_folder 호출 시 갱신됨)
   - `backupfolders.json`: standardObjectDirs 1개 + 다른 4개
     storage class array는 빈 배열

4. **dedup 시드** (`dedup_against_existing=True`인 경우):
   - `arq_writer.dedup.seed_existing_destination(...)` 호출
   - 두 가지 시딩:
     a. `seed_from_standardobjects`: backend.list_dir로
        `standardobjects/<shard>/<file>` 전체를 walk. 파일명이
        62-hex pattern이면 `blob_id = shard + filename`을 얻고
        `BlobLoc(isPacked=False, relativePath=..., length=stat_size)`로
        캐시에 추가.
     b. `find_latest_backuprecord_per_folder`: 각 폴더의 최신
        backuprecord 경로를 모음. 각각에 대해
        `seed_from_backuprecord(rec_path, cache,
        encryption_key, hmac_key, dest_root, backend)` 호출:
        - backuprecord ARQO를 read_all + 복호 + LZ4-unwrap +
          plistlib.loads
        - 루트 Node dict 추출 → 만약 isTree=True면
          `_harvest_tree_recursive`로 모든 자식 Tree blob을
          fetch + 복호 + parse_tree, BlobLoc들을 모두 수집
        - packed 모드에서 `<cu>/blobpacks/<shard>/<UUID>.pack`을
          가리키는 BlobLoc도 캐시에 들어감 → 다음 실행에서 그 blob을
          다시 만들지 않게 됨

### 1.3 `add_folder(source)` — 한 폴더 백업

`Backup.add_folder` (`arq_writer/backup.py`):

1. **폴더 디렉터리 생성**:
   - `<cu>/backupfolders/<folder_uuid>/`
   - `backupfolder.json` 작성 (localPath, name, uuid 등)

2. **backupplan.json 갱신**:
   - `_folder_plans`에 새 항목 append (build_folder_plan 결과)
   - `_write_plan_json()` 호출 → backupplan.json을 다시 씀

3. **PriorTreeIndex 빌드** (`dedup_against_existing` + 키셋 재사용):
   - `arq_writer.prior_tree.PriorTreeIndex(dest_root,
     computer_uuid, encryption_key, hmac_key, folder_uuid,
     backend)`
   - 가장 최근 backuprecord를 찾아 root Node의 treeBlobLoc을
     얻음. 이후 `lookup_file(rel_path)` 호출 시점에 lazy하게 해당
     경로까지 트리 blob들을 fetch + parse하여 prior FileNode를
     반환. Tree blob들은 blob_id로 캐싱.

4. **소스 트리 walk** (`_walk` → `_walk_dir` / `_walk_file`):

   재귀 호출의 매 directory boundary에서 `_check_cancel()`로
   협조 취소 플래그를 검사 (BackupCancelled 예외 발생 가능).

   **`_walk_file(src, rel_path)`**:
   - **PriorTreeIndex 히트 검사**: rel_path를 prior tree에서
     찾고, 발견된 FileNode의 `(mtime_sec, mtime_nsec, itemSize,
     mac_st_mode & 0o7777)`이 현재 src의 stat과 일치하면
     dataBlobLocs를 그대로 재사용 → `read_bytes` / 청커 / 해시
     모두 스킵 → `files_reused += 1` + `file_reused` 콜백 emit.
   - **그 외 (변경된 파일 또는 prior 없음)**:
     - `data = src.read_bytes()`
     - 청커 활성: `Buzhash(config).chunk(data)` 호출
       - 내부 알고리즘 (`arq_writer/chunker.py`):
         a. data가 `min_chunk_size` 이하면 한 덩어리 그대로 yield
         b. 그렇지 않으면 첫 `min_chunk_size` 만큼 진행한 후
            `window_size` 바이트 윈도우에 대해 Buzhash 초기 해시
            계산
         c. 한 바이트씩 슬라이드하며 `H = ROL(H_old, 1) ⊕
            ROL(T[byte_out], n) ⊕ T[byte_in]` 갱신
         d. `H & boundary_mask == 0`이면 청크 경계로 cleave →
            yield, 다음 청크 시작
         e. `max_chunk_size`까지 도달했는데 경계 못 찾으면 강제
            cleave
     - 청커 비활성: 전체를 한 청크로
     - 각 청크에 대해 `_write_blob(piece)` → BlobLoc 반환 →
       `dataBlobLocs`에 추가
   - **FileNode 빌드**: dataBlobLocs + (mtime/ctime/mode/uid/
     gid/nlink/itemSize/...)
   - `files_written += 1` + `file_written` 콜백

   **`_walk_dir(src, rel_path)`**:
   - 자식들을 정렬해서 차례로 `_walk(child)` 재귀
   - 자식들의 `(name, Node)` 튜플들을 모아 `Tree(children=...,
     version=3)` 빌드
   - `serialize.write_tree(tree, version=3)`로 binary 직렬화
     (network byte order, [String] 8-byte length prefix 등 spec
     convention 그대로)
   - `_write_blob(tree_bytes, is_tree=True)` → 트리 blob 기록 →
     반환된 BlobLoc으로 `TreeNode(treeBlobLoc=..., itemSize=...,
     containedFilesCount=..., 디렉터리의 mtime/ctime/mode/...)`
     빌드

   **`_write_blob(plaintext, is_tree=False)`**:
   - `blob_id = compute_blob_id(blob_id_salt, plaintext)` 계산
   - `_written_blobs.get(blob_id)`이 hit이면 캐시된 BlobLoc 즉시
     반환 (지연 없이 dedup)
   - miss면:
     a. `lz4_wrap(plaintext)`: 4B BE original_length + LZ4 block
     b. `build_encrypted_object(lz4_bytes, encryption_key,
        hmac_key)`:
        - 32B random session_key, 16B random data_iv, 16B random
          master_iv 생성
        - encrypted_session = AES-256-CBC(encryption_key,
          master_iv, data_iv ‖ session_key)
        - ciphertext = AES-256-CBC(session_key, data_iv,
          lz4_bytes)
        - body = master_iv ‖ encrypted_session ‖ ciphertext
        - mac = HMAC-SHA256(hmac_key, body)
        - return `b"ARQO" + mac + body`
     c. **표준 객체 모드** (`use_packs=False`):
        - 경로 = `/<cu>/standardobjects/<blob_id[:2]>/<blob_id[2:]>`
        - backend.mkdir(parent) → backend.write_all(path, arqo)
        - BlobLoc(isPacked=False, relativePath=path, offset=0,
          length=len(arqo))
     d. **패킹 모드** (`use_packs=True`):
        - tree blob이면 `_tree_pack` (lazy 생성), 데이터 blob이면
          `_blob_pack`
        - PackBuilder.add(blob_id, arqo):
          - 첫 호출 시 새 pack 경로 할당 (UUID 기반)
          - in-memory 버퍼에 ARQO 바이트를 그대로 append
          - 버퍼가 `max_pack_bytes` (기본 10 MiB) 초과하면
            backend.write_all로 flush + 다음 pack 시작
          - BlobLoc(isPacked=True,
                   relativePath=current_pack_path,
                   offset=offset_in_buffer, length=len(arqo))
   - `_written_blobs[blob_id] = loc` + 카운터 갱신

5. **flush_packs()**: in-flight pack 버퍼들을 모두 디스크에
   기록. backuprecord에 들어가는 BlobLoc의 offset이 실제 디스크
   상의 위치를 가리키도록 보장.

6. **backuprecord 작성**:
   - bucket = `f"{int(time):05d/100000:05d}"`, rec_num = `int(time) %
     100000`
   - 디렉터리 `<cu>/backupfolders/<fu>/backuprecords/<bucket>/`
     mkdir
   - `build_backuprecord_dict(...)`로 dict 빌드:
     - node = root TreeNode를 dict로 변환 (treeBlobLoc도 dict로)
     - creationDate, arqVersion, computerOSType,
       backupFolderUUID, backupPlanUUID, backupPlanJSON
       (현재 plan 스냅샷), version=100, isComplete=True 등
   - `serialize_backuprecord(record_dict)`: `plistlib.dumps(...,
     fmt=plistlib.FMT_BINARY)`로 binary plist 직렬화
   - `build_backuprecord_arqo(plist_bytes, ...)`: LZ4-wrap +
     ARQO 외피
   - backend.write_all로 `<bucket>/<rec_num>.backuprecord`에 기록

7. **정상 반환**: `rec_path` (Path 또는 backend-relative). 단,
   walk 도중 `BackupCancelled` 발생 시:
   - `flush_packs`가 호출되지 않음 → in-memory pack 버퍼 손실 (이미
     디스크에 flush된 pack은 valid blob들의 strict subset이라
     해롭지 않음)
   - backuprecord 작성 안 됨 → destination의 prior 상태가 그대로
     유지됨 (consistent)

### 1.4 호출 그래프 요약

```
build_backup
└── Backup.__init__              (키 결정 + backend 설정)
└── Backup.init_plan
    ├── backend.mkdir × N
    ├── build_encrypted_keyset   (PBKDF2 + AES-CBC + HMAC)
    ├── backend.write_all        (encryptedkeyset.dat)
    ├── build_backupconfig + write_all
    ├── build_backupfolders_json + write_all
    ├── build_backupplan + write_all
    └── seed_existing_destination (옵셔널)
        ├── seed_from_standardobjects
        └── for each folder:
            └── seed_from_backuprecord
                └── _harvest_tree_recursive (재귀 트리 walk)
└── Backup.add_folder
    ├── backend.mkdir (folder dir)
    ├── build_backupfolder_json + write_all
    ├── build_folder_plan + _write_plan_json
    ├── PriorTreeIndex(...)      (옵셔널)
    └── _walk(source, "")
        └── _walk_dir / _walk_file (재귀)
            ├── prior_tree.stat_matches → reuse
            ├── chunker.chunk → 청크들
            └── _write_blob × N
                ├── compute_blob_id
                ├── lz4_wrap
                ├── build_encrypted_object
                └── backend.write_all OR PackBuilder.add
    ├── flush_packs
    ├── build_backuprecord_dict + arqo
    └── backend.write_all (backuprecord)
```

### 1.5 어떤 파일이 디스크에 쓰이는가

가장 단순한 백업 (`source/a.txt`, standalone 모드):

```
<dest>/<CU>/encryptedkeyset.dat
<dest>/<CU>/backupconfig.json
<dest>/<CU>/backupplan.json
<dest>/<CU>/backupfolders.json
<dest>/<CU>/backupfolders/<FU>/backupfolder.json
<dest>/<CU>/standardobjects/<a.txt blob_id[:2]>/<...>     # a.txt 본문
<dest>/<CU>/standardobjects/<root tree blob_id[:2]>/<...> # 루트 트리
<dest>/<CU>/backupfolders/<FU>/backuprecords/<bucket>/<num>.backuprecord
```

패킹 모드라면 standardobjects/ 두 줄이 사라지고 대신:

```
<dest>/<CU>/blobpacks/<2-hex>/<UUID>.pack          # a.txt + 다른 blobs
<dest>/<CU>/treepacks/<2-hex>/<UUID>.pack          # 트리들
```

---

## 2. 검증 흐름

검증은 두 갈래로 제공:

- **`arq_validator.validate(backend, root, *, tier=...)`** — 4 tier
  계층 검증 (L0/L1a/L1b/L2). 라이브러리 + CLI (`arq-validator`).
- **`arq_validator.check_arq7_compatibility(backend, root, *,
  encryption_password=...)`** — 형식 conformance 단일 함수. 25개의
  spec invariant를 모두 점검.

두 함수 모두 형식/해시 실패에 대해 raise하지 않습니다. 결과 객체
(`ValidationReport` / `ComplianceReport`)에 모든 발견을 담아 반환.

### 2.1 Tier 계층 (`arq_validator/tiers.py`)

각 상위 tier는 하위 tier들을 모두 포함합니다. 비용이 누적적.

#### 2.1.1 L0 (DRY_RUN) — 레이아웃 모양

`run_l0(backend, root, callback=None)`:

1. backend.list_dir(root) → 8-4-4-4-12 hex UUID 패턴에 매치되는
   디렉터리들을 컴퓨터 UUID로 인식 (`COMPUTER_UUID_RE`)
2. 각 컴퓨터에 대해 `discover_layout`이 다음을 수집:
   - keyset 파일 존재 여부
   - 4개 object family 디렉터리 (standardobjects/treepacks/
     blobpacks/largeblobpacks)와 그 내부 shard 카운트
   - 각 backupfolders/<FU>/backuprecords/ 안의 record 파일 개수
3. 결과: `LayoutResult(layout_ok=bool, computers=[...])`

이 단계는 어떤 암호화도 풀지 않습니다. 백엔드 round-trip latency만
영향을 주는 경량 검증.

#### 2.1.2 L1a (QUICK) — ARQO magic 표본

`run_l1a(backend, layouts, root, sample_fraction=0.05)`:

1. 각 object family에서 표본 추출 (기본 5%)
2. 각 표본에 대해 `backend.read_range(path, 0, 4)` → 첫 4 byte 읽음
3. `ARQO_MAGIC` (b"ARQO") 비교
4. 불일치하는 파일 수 + 경로 기록

용도: 비트로트 / 부분 전송 / 0-byte 파일을 cheap하게 검출.

#### 2.1.3 L1b (DEEP) — 키셋 + 최신 backuprecord HMAC

`run_l1b(backend, layouts, root, encryption_password, openssl_path)`:

1. 각 컴퓨터에 대해 keyset 복호:
   - `parse_keyset_storage(blob)`:
     - 처음 25 byte가 `ARQ_ENCRYPTED_MASTER_KEYS`인지 확인
     - 25..33 = salt, 33..65 = stored_mac, 65..81 = iv, 81..end = ct
   - `derived = pbkdf2_hmac_sha256(password, salt, 200_000,
     dklen=64)`
   - `aes_key = derived[:32]`, `mac_key = derived[32:]`
   - `actual_mac = HMAC-SHA256(mac_key, iv ‖ ct)` 비교 → 불일치
     시 "wrong password OR file corruption"으로 분류
   - `plaintext = AES-256-CBC-decrypt(aes_key, iv, ct)` (호스트
     `openssl enc -d` 호출)
   - `parse_keyset_plaintext`: version=3 + 3 × (uint64 length +
     32-byte field) 형식 검증 후 (encryption_key, hmac_key,
     blob_id_salt) 반환
2. 각 폴더의 최신 backuprecord에 대해:
   - `find_latest_backuprecord(backend, root, cu, fu)`로 path 결정
   - ARQO 본문 읽기
   - `verify_arqo_hmac(arqo, hmac_key)`: 매직 + 길이 + body[36:]에
     대해 HMAC 검증
3. 결과: `BackupRecordResult(total, ok, fail, failures=[...])`

비밀번호와 가장 최근 record 한 개만 사용. L1a보다 깊지만 여전히
빠름 (전체 수많은 blob들을 HMAC하지 않음).

#### 2.1.4 L2 (AUDIT) — 전체 HMAC 일소

`run_l2(backend, layouts, keyset, root, audit_skip_larger_than=...,
audit_max_runtime_sec=..., audit_max_bytes=...)`:

1. 각 object family에 대해 모든 파일 walk
2. 각 파일에 대해 `read_all` → ARQO HMAC 검증
3. pack 파일은 추가로 BlobLoc index를 따라가며 슬라이스마다 HMAC
   (하지만 실제로는 pack 내부 ARQO 단위가 아닌 pack 파일 전체에
   대해 한 번)
4. 옵션:
   - `audit_skip_larger_than`: 매우 큰 blob 스킵
   - `audit_max_runtime_sec` / `audit_max_bytes`: 부분 audit (early
     exit)
5. 결과: `ObjectAuditResult(files_total, files_ok, files_fail,
   bytes_ok, ...)`

대용량 destination에서 시간이 오래 걸리므로 `audit_drip` 패턴이
별도 제공됨.

#### 2.1.5 audit-drip — 재개식 L2

`arq_validator.run_audit_drip(backend, target, state_file,
encryption_password, max_runtime_sec, rate_files_per_min, ...)`:

1. `state_file`에서 cursor 로드 (이전 fire에서 멈춘 지점)
2. cursor부터 walk 재개
3. 각 fire는 `max_runtime_sec` 또는 `audit_max_bytes` 제한 내에서
   진행 → 끝나기 전 종료 시 cursor + 카운터를 state file에 저장
4. throttle: `rate_files_per_min`이 주어지면 파일 처리 사이에
   sleep 삽입

**용도**: NAS / 클라우드 storage box처럼 read 쓰루풋이 제한된
환경에서 매일 야간 등 짧은 윈도우로 분할 audit. 중단되어도
손실 없이 다음 회차에 정확히 cursor에서 재개.

### 2.2 형식 conformance 검증 (`arq_validator.compatibility`)

`check_arq7_compatibility(backend, root, *, encryption_password,
computer_uuid=None)`:

25개의 spec invariant를 한 번에 점검. 각 invariant는 stable id
(L1~L8 / C1~C4 / A1~A2 / B1~B3 / P1~P2 / S1 / ID1~ID2 / SV1~SV3)와
함께 `CheckResult` 객체로 보고됨. 자세한 invariant 표는
`docs/COMPATIBILITY.md`.

흐름:

1. **L1**: top-level 컴퓨터 UUID 디렉터리 1개 이상 존재 확인
2. **L2 + C1~C4**: keyset 파일 존재 / magic / 레이아웃 / 복호 +
   HMAC / plaintext 모양
3. **L3 + SV1~SV2**: backupconfig.json 모든 필수 키 + 타입 +
   chunkerVersion ∈ {1, 2, 3} + blobIdentifierType ∈ {1, 2}
4. **L4**: backupplan.json 모든 필수 키 (planUUID,
   backupFolderPlansByUUID, scheduleJSON 등) + 폴더 plan 항목의
   필수 키
5. **L5**: backupfolders.json 5개 storage class array
6. **L6 + L7 + L8**: 폴더 디렉터리 + backupfolder.json + record
   path 모양
7. **B1~B3 + SV3**: 각 backuprecord의 ARQO + plist 파싱 + 9개
   필수 키 + node 모양 + version ∈ {100, 200}
8. **A1~A2 + S1 + ID1~ID2**: standardobjects/에서 최대 32개 sample
   추출 → ARQO + HMAC + 파일명 정규식 + `blob_id == SHA-256(salt ‖
   plaintext)` 검증
9. **P1~P2**: pack 파일이 있다면 UUID 형식 이름 + offset 0의 ARQO
   magic

각 단계는 try/except로 감싸져 있어 한 invariant의 실패가 다음
invariant 검증을 막지 않음. 모든 발견을 `ComplianceReport`에 모아
반환 → 호출자가 `report.passed` / `report.failed_checks`로 처리.

---

## 3. 복원 흐름

CLI 진입: `arq-reader` (`arq_reader.cli`) 또는 TUI의
`RestoreRunScreen`. 프로그래밍 진입: `arq_reader.Restore`.

### 3.1 `Restore.__init__` — backend 결정

`arq_reader/restore.py`:

```python
Restore(src, encryption_password, *, backend=None, openssl_path=...)
```

- `backend=None`: `LocalBackend(Path(src).resolve())` 생성
- `backend` 인자: 그대로 사용. `src`는 backend-namespace path
  (보통 SFTP 서버상의 path 또는 그냥 `"/"`)

password는 클래스 인스턴스 동안 메모리에만 보관 (디스크에 안 씀).

### 3.2 Layout 발견

`Restore.layouts()`:

```python
self._layouts = discover_layout(self.backend, "/")
```

L0과 동일하게 컴퓨터 UUID 디렉터리들을 walk. 각
`Arq7ComputerLayout`은 `computer_uuid`, `backup_folder_uuids`,
object family 디렉터리들의 존재 여부 등을 노출. lazy하게 한 번만
계산하고 캐시.

### 3.3 `Restore.restore(...)` — 한 폴더 복원

진입점:

```python
restore(*, folder_uuid, dest, computer_uuid=None,
        backuprecord_path=None, paths=None, callback=None)
```

1. **컴퓨터 결정**: `computer_uuid`가 None이면 `_resolve_single_
   computer(folder_uuid)`로 layouts에서 단일 컴퓨터 검색
   (모호 / 부재 시 `ValueError`)

2. **keyset 로드**: `self.keyset(computer_uuid)`:
   - 캐시 (`_keyset_by_computer`) 확인
   - 캐시 miss: encryptedkeyset.dat 읽고 `decrypt_keyset(blob,
     password)` (L1b와 동일 절차) → 캐시에 저장
   - `Keyset(encryption_key, hmac_key, blob_id_salt)` 반환

3. **backuprecord 결정**:
   - `backuprecord_path`가 주어지면 그대로 사용 (특정 시점 복원)
   - 그렇지 않으면 `find_latest_backuprecord(backend, "/", cu, fu)`
     → 각 폴더의 chronologically 최신 record 경로

4. **path filter 빌드** (`paths` 인자가 None이 아니면):
   - `_build_path_filter(paths)`로 `_PathFilter` 생성
   - 각 path는 strip된 슬래시 형태로 보관
   - `matches(rel_path)`: 정확 일치 또는 prefix 일치 (디렉터리
     마크용)
   - `descend(rel_path)`: 해당 디렉터리에 매칭되는 자손이 있을
     수 있는지 → walk 시 무관한 서브트리를 건너뛸 수 있게 해줌

5. **backuprecord 복호**:
   - `self.backend.read_all(record_path)` → ARQO 바이트
   - `decrypt_lz4_arqo(arqo, encryption_key, hmac_key,
     openssl_path=...)`:
     a. `decrypt_encrypted_object(arqo, ...)` 내부:
        - magic 4 byte + HMAC 32 byte + body 검증
        - body[0:16] = master_iv
        - body[16:80] = encrypted_session (64B)
        - body[80:] = ciphertext
        - `data_iv ‖ session_key = AES-256-CBC-decrypt(encryption_
          key, master_iv, encrypted_session)`
        - `plaintext = AES-256-CBC-decrypt(session_key, data_iv,
          ciphertext)`
     b. `lz4_unwrap(plaintext)`: 4B BE length + LZ4 block
        decompress
   - `plistlib.loads(plist_bytes)` → record dict
   - `record["node"]` 추출

6. **루트 노드 처리 분기**:
   - **루트가 TreeNode** (`isTree=True`):
     `_restore_dir_node(tree_blob_loc, out_dir, keyset, result,
     callback, rel_path="", path_filter, check_cancel)` 재귀.
   - **루트가 FileNode** (드물지만 가능):
     `_restore_file_node(file_node, out_dir, ...)` 호출.

### 3.4 `_restore_dir_node` — 디렉터리 복원

각 호출에서:

1. `path_filter.descend(rel_path)`로 이 서브트리에 매칭이
   있을 수 있는지 확인. 없으면 즉시 return (중요한 최적화 — 트리
   blob fetch 자체를 건너뜀).
2. `out_dir.mkdir(parents=True, exist_ok=True)` (실제 로컬 파일
   시스템 디렉터리 생성)
3. `tree_bytes = self._fetch_blob(tree_blob_loc, keyset)`:
   - `loc.isPacked=True`: `backend.read_range(loc.relativePath,
     loc.offset, loc.length)`로 pack 내부 ARQO만 슬라이스
   - `loc.isPacked=False`: `backend.read_all(loc.relativePath)`
   - 첫 4 byte가 `ARQO`면 `decrypt_encrypted_object` (HMAC 검증
     + 복호)
   - `loc.compressionType == 2`: `lz4_unwrap`
   - `loc.compressionType == 1`: stdlib `gzip.decompress` (Arq 5
     legacy)
   - `loc.compressionType == 0`: 그대로
4. `parse_tree(tree_bytes)`: spec convention의 binary parser →
   `Tree(version, children)` 반환
5. 각 자식에 대해:
   - `child_rel = f"{rel_path}/{child.name}"` (rel_path가 빈
     문자열이면 그냥 child.name)
   - **TreeNode**: `_restore_dir_node(child.node.treeBlobLoc,
     out_dir / child.name, ..., child_rel, path_filter)` 재귀
   - **FileNode**:
     - `path_filter.matches(child_rel)`이 False면 skip
     - `_restore_file_node(child.node, out_dir / child.name, ...)`

### 3.5 `_restore_file_node` — 단일 파일 복원

1. `out_path.parent.mkdir(parents=True, exist_ok=True)`
2. 모든 dataBlobLocs를 in-order로 fetch (`_fetch_blob` 호출):
   - 각 blob → ARQO 검증 + 복호 + (LZ4/gzip) 압축 해제
3. `chunks = [bytes_per_blob, ...]`
4. `out_path.write_bytes(b"".join(chunks))` — 청크들을 concat하여
   원본 재조립
5. `os.utime(out_path, (mtime, mtime))` — mtime 복구 (실패는 비치명적
   처리)
6. `result.files_restored += 1` + `file_restored` 콜백

**현재 미구현 사항** (`docs/COVERAGE.md` ⚠️ / ❌):
- 심볼릭 링크: FileNode의 mode 비트는 보존되지만 실제 `os.symlink`
  호출은 없음 → 보통 파일로 복원됨
- xattr / ACL 적용: Node에 보존되어 있지만 실제 `setxattr` /
  `setfacl`은 호출 안 함
- 하드링크: 별도 파일로 복원 (Arq.app도 동일)
- 소유권 (`mac_st_uid` / `mac_st_gid`): 메타데이터에 보존, 복원
  시 적용 안 함

### 3.6 호출 그래프 요약

```
Restore.restore
├── _resolve_single_computer / 명시 인자
├── self.keyset(computer_uuid)
│   └── decrypt_keyset (PBKDF2 + AES-CBC + HMAC)
├── find_latest_backuprecord OR backuprecord_path 사용
├── _build_path_filter (옵셔널)
├── backend.read_all (record)
├── decrypt_lz4_arqo
│   ├── decrypt_encrypted_object
│   │   ├── HMAC-SHA256 검증
│   │   ├── AES-256-CBC-decrypt × 2 (session + body)
│   ├── lz4_unwrap
├── plistlib.loads → record dict
├── _restore_dir_node (root tree)
│   ├── path_filter.descend (early exit)
│   ├── out_dir.mkdir
│   ├── _fetch_blob (tree blob)
│   │   ├── backend.read_range / read_all
│   │   ├── decrypt_encrypted_object
│   │   └── lz4_unwrap
│   ├── parse_tree
│   └── for each child:
│       └── _restore_dir_node (재귀) / _restore_file_node
└── _restore_file_node
    ├── for each dataBlobLoc: _fetch_blob (decrypt + decompress)
    ├── concat chunks → out_path.write_bytes
    └── os.utime
```

### 3.7 어떤 일이 다른 모드에서 일어나는가

**SFTP destination**:
- 모든 `backend.read_all` / `read_range`가 SSH master를 경유한
  `head -c N` / `dd skip=K count=N` 또는 `sftp get`으로 변환됨
- 마스터 SSH 세션 1개 위에서 모든 blob fetch가 multiplex됨 → 매
  blob마다 새 TCP/SSH handshake 비용 없음
- pack 파일에서의 부분 읽기 (`read_range`)도 동일 마스터를 사용

**packed 모드 destination**:
- 각 BlobLoc은 `(pack_path, offset, length)` 트리플
- `read_range`가 pack 파일의 정확한 슬라이스만 가져옴 → pack 전체를
  내려받지 않음
- 한 pack에 여러 파일의 청크들이 섞여 있어도 BlobLoc.offset이
  정확히 그 청크의 위치를 가리킴

**path-filtered 복원**:
- `paths=["문서/이력서.txt", "사진"]`처럼 부분 경로 지정
- byte-for-byte UTF-8 비교 → 비ASCII 경로명도 그대로 매칭
- `descend`가 무관한 서브트리의 트리 blob fetch를 통째로 스킵 →
  대용량 백업의 부분 복원이 효율적

**historical record 복원**:
- `Restore.list_records(folder_uuid)`로 record 히스토리 조회
- 각 `RecordInfo`의 `relative_path`를 `restore(...,
  backuprecord_path=...)`로 전달 → 특정 시점 스냅샷 복원

---

## 4. 한 줄 요약

| 흐름 | 입력 | 핵심 단계 | 출력 |
|------|------|----------|------|
| **백업** | 소스 트리 + 비밀번호 + destination | walk → 청크 → ARQO 외피 → backend.write_all | `<dest>/<CU>/...` 디렉터리 트리 + backuprecord |
| **검증** | destination + (옵션) 비밀번호 | tier별 점검 (layout / magic / HMAC / 전체 audit) OR 25개 형식 invariant | `ValidationReport` 또는 `ComplianceReport` |
| **복원** | destination + 비밀번호 + 대상 디렉터리 | keyset 복호 → backuprecord 복호 → 트리 walk → 각 blob fetch+복호+LZ4풀이 → concat → 파일 작성 | 로컬 파일시스템에 재구성된 파일 트리 |

세 흐름의 공통 분모:

- **byte-level 형식**: ARQO 외피 + LZ4 wrap + binary plist /
  binary Tree + JSON sidecar
- **content addressing**: SHA-256(salt ‖ plaintext) 16진 64자
- **암호화**: AES-256-CBC + HMAC-SHA256 + PBKDF2-SHA256
- **storage**: backend Protocol 6+2 메서드 위에서 모두 동작 →
  Local / NAS / SFTP가 동일 코드 경로

---

## 부록 A. 주요 모듈 빠른 참조

| 모듈 | 무엇을 하는가 |
|------|--------------|
| `arq_writer.backup` | 백업 orchestrator (Backup 클래스 + build_backup) |
| `arq_writer.crypto_write` | encryptedkeyset.dat 빌드, ARQO 빌드, blob_id 계산, `rotate_keyset_password` |
| `arq_writer.serialize` | binary Tree / Node / BlobLoc 직렬화 |
| `arq_writer.json_configs` | backupconfig / backupplan / backupfolder JSON |
| `arq_writer.backuprecord` | binary plist backuprecord 빌드 |
| `arq_writer.lz4_block` | 4B-prefix LZ4 wrap / unwrap |
| `arq_writer.chunker` | Buzhash content-defined chunker |
| `arq_writer.arq_chunker_params` | Arq.app v7.41 RE된 청커 파라미터 |
| `arq_writer.pack_builder` | treepacks/blobpacks/largeblobpacks 빌더 |
| `arq_writer.dedup` | cross-run dedup 시드 helper들 |
| `arq_writer.prior_tree_index` | tree-walk reuse용 PriorTreeIndex |
| `arq_writer.exclusions` | `ExclusionRules` (glob + regex + .gitignore-subset) |
| `arq_writer.macos_snapshot` | macOS APFS 스냅샷 컨텍스트 매니저 (`with_apfs_snapshot`) |
| `arq_writer.retention` | `RetentionPolicy` + `prune_records` + `gc_orphan_blobs` + `apply_retention` |
| `arq_validator.crypto` | keyset 복호 + HMAC 검증 + ARQO 복호 |
| `arq_validator.tiers` | L0 / L1a / L1b / L2 구현 |
| `arq_validator.runner` | 4 tier orchestrator (`validate(...)`) |
| `arq_validator.audit_drip` | 재개식 L2 sweep |
| `arq_validator.compatibility` | 25개 spec invariant checker |
| `arq_validator.layout` | computer/folder discovery + record path |
| `arq_validator.backend` | Backend Protocol + LocalBackend |
| `arq_validator.sftp` | SftpBackend (SSH master + sftp put/rename) |
| `arq_reader.restore` | Restore 클래스 + 복원 walk |
| `arq_reader.decrypt` | ARQO 복호 헬퍼 (write 측 inverse) |
| `arq_reader.parse` | binary Tree / Node / BlobLoc 파서 |

---

## 부록 B. 오류가 어디서 어떻게 발생하나

| 증상 | 가능한 원인 | 어떤 함수에서 잡히는가 |
|------|-------------|----------------------|
| `DecryptError: HMAC mismatch` | 비밀번호 오답 OR keyset 변조 | `decrypt_keyset` (verify 단계) |
| `DecryptError` (record) | hmac_key 불일치 OR blob 변조 | `verify_arqo_hmac` |
| `lz4 unwrap failed` | 압축 데이터 손상 OR length prefix 변조 | `lz4_unwrap` |
| `parse_tree: bad version` | 트리 blob 변조 / 버전 불일치 | `parse_tree` |
| `BackupCancelled` | `Backup.cancel()` 호출 후 walk 도중 | `_walk` (`_check_cancel`) |
| `ValueError: folder UUID not found` | 잘못된 folder_uuid | `Restore._resolve_single_computer` |
| `RuntimeError: ssh master ... not ready` | SFTP 연결 실패 | `SftpBackend.__enter__` |

세 흐름 모두 best-effort 오류 처리: 단일 blob 손상이 전체 백업 /
audit / 복원을 막지 않고, `failures` 리스트에 발견을 모아 사용자
판단에 맡깁니다.

---

## 부록 C. 유지보수 흐름 (PR #11–#12)

위 § 1–3 은 작성·검증·복원 세 흐름을 다룹니다. PR #11 / #12 에서 추가된
**유지보수 작업** 두 가지의 동작은 다음과 같습니다.

### C.1 비밀번호 회전 — `rotate_keyset_password`

- 입력: 기존 `encryptedkeyset.dat` 의 raw bytes + old/new 비밀번호
- 절차:
  1. `decrypt_keyset(blob, old_password)` → `(encryption_key, hmac_key, blob_id_salt)` 추출
  2. 새 8 바이트 salt + IV 생성
  3. `build_encrypted_keyset(new_password, encryption_key, hmac_key, blob_id_salt)` 로
     동일 마스터 키 + 새 salt/IV 로 재암호화
- 결과: 마스터 키 변동 없음 → 모든 기존 backuprecord / blob 그대로 복호화 가능.
  새 keyset bytes 만 destination 에 다시 쓰면 됨 (`backend.write_all(...)`).

### C.2 보존·가지치기·blob GC — `apply_retention`

- 입력: backend + 비밀번호 + `RetentionPolicy` (keep_last_n + 시간 버킷 5종)
- 1단계 `prune_records()`:
  - 모든 `<CU>/backupfolders/<folder>/backuprecords/...backuprecord` 를 enumerate
  - `select_retained()` 가 정책에 따라 보존 집합 결정 (시간 버킷은 OR 결합)
  - 보존 외 record 들을 `backend.unlink(path)` 로 삭제
- 2단계 `gc_orphan_blobs()` (선택):
  - 보존된 모든 record 의 트리를 walk → 참조된 standalone blob ID 집합 + 참조된 pack 경로 집합 수집
  - `<CU>/standardobjects/<2hex>/<60hex>` 중 참조 집합에 없는 blob 삭제
  - `<CU>/treepacks/`/`blobpacks/`/`largeblobpacks/` 중 path 가 참조 pack 집합에 없는 pack 만 삭제
    (보수적 — pack 내부 일부만 orphan 이어도 그 pack 은 보존)
- 콜백 이벤트: `record_deleted` / `blob_deleted` / `pack_deleted` (dry-run 모드에서도 동일하게 emit)

TUI 의 `MaintenanceScreen` (`arq_tui/screens/maintenance.py`) 가 양쪽 모두 sibling
스레드로 호출하고, 결과는 `call_from_thread` 로 메인 루프에 marshal 합니다.
