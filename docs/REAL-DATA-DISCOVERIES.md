# Real-SFTP-data 검증으로 발견·개선한 호환성 사항

> **요약**: 합성 (synthetic) 단위 테스트만으로는 통과하던 reader / writer /
> validator 가, 실 Arq.app v8 destination (Hetzner Storage Box) 을 대상으로
> 검증을 시작하자 **4개의 비호환 영역과 1개의 성능 병목**을 즉시 노출시켰습니다.
> 모두 부분 fix 가 아닌 양방향 호환 fix 로 처리해 향후 Arq.app round-trip
> 가능성을 확보했습니다.

이 문서는 **Before / After** 형식으로 각 차이점과 그 영향, 그리고 fix 의
근거를 기록합니다. `.secrets/` 자격증명을 통해 실 destination 에 연결한
순간부터의 발견 사항이며, 동일한 fix 가 없으면 `arq_restore` (BSD 참조 구현)
나 Arq.app GUI 가 우리 writer 의 출력을 read 하지 못합니다.

## 0. 검증 환경

- 운영자 실 destination: Hetzner Storage Box (chrooted SFTP-only 서버)
- Arq.app 기록자: v8.x (destination 의 `arqVersion` 필드 기준)
- Computer UUID: 1개, Backup folders: 5개
- Sharded 디렉토리: standardobjects/treepacks/blobpacks/largeblobpacks 각 256 shards
- 자격증명 채널: `.secrets/sftp.json` (identity_file 또는 password) +
  `.secrets/dest_password` (Arq 암호화 비밀번호)

테스트 진입점:
- `tests/integration/test_arqapp_sftp_compat.py` — 형식·형상 검증 (PR #9)
- `tests/integration/test_arq_real_destination.py` — 런타임 reader/validator/writer (PR #16)
- `tests/integration/test_arq_real_destination_deep.py` — 포맷 invariant 자동 발견 (이번 작업)

## 1. SftpBackend 가 chrooted SFTP-only 서버에서 모두 실패

### Before

`SftpBackend` 의 7개 메서드 (`is_dir`, `exists`, `stat_size`, `read_range`,
`mkdir`, `unlink`, `write_all` 의 partial cleanup) 가 SSH 임의 명령에 의존:

```python
def is_dir(self, path):
    cp = self._run_ssh(f"test -d {shlex.quote(path)} && echo Y || echo N")
    return cp.returncode == 0 and cp.stdout.decode().strip() == "Y"
```

합성 (LocalBackend mock) 테스트만 보면 정상이었지만, Hetzner Storage Box 처럼
chrooted SFTP-only 서버에서는 **모든 SSH 명령이 거부**됩니다:

```
ssh ... -- test -d /home/...
→ rc=8
→ stderr: "Command not found. Use 'help' to get a list of available commands."
```

이 한 줄로 layout discovery, restore, validate, writer 의 destination 초기화
모두 실패. 합성 테스트는 LocalBackend 만 사용해 이 경로를 한 번도 타지 않았기
때문에 잡히지 않음.

### After (commit `792e521`)

모든 7개 메서드를 sftp 프로토콜로 재작성:

| 메서드 | 새 구현 |
|---|---|
| `is_dir(path)` | `sftp cd <path>` (rc=0 ⇒ dir, rc=1 ⇒ file/missing) |
| `exists(path)` | `sftp cd <path>` 또는 `sftp ls -l <path>` 중 하나라도 성공 |
| `stat_size(path)` | `sftp ls -l <path>` 의 5번째 컬럼 |
| `read_range(path, off, len)` | ssh `head -c`/`dd` 시도 → rc≠0 이면 sftp `get` 전체 다운로드 후 메모리 슬라이스 |
| `mkdir(path, parents)` | sftp `mkdir` 으로 ancestor 단계별 생성, `cd` probe 로 이미 존재 여부 검사 |
| `unlink(path)` | sftp `rm`, "No such file" stderr 는 silent OK (rm -f 시맨틱) |
| `write_all` partial cleanup | `_run_sftp_batch("rm <partial>\nbye\n")` |

검증: `test_layout_discovers_computer`, `test_keyset_decrypts` 등 운영자
destination 전부 `OK`.

## 2. Backuprecord 는 binary plist 가 아닌 UTF-8 JSON

### Before

우리 writer 와 reader 는 spec 의 "binary plist" 표현을 따라 다음 라인을
사용했습니다:

```python
# writer
return plistlib.dumps(record, fmt=plistlib.FMT_BINARY)

# reader
record = plistlib.loads(record_plain)
```

자체 round-trip 단위 테스트는 양방향 모두 binary plist 라 통과.

### After (commit `399480a`)

운영자 record 의 첫 80바이트를 디코드하면:

```
b'{"backupFolderUUID":"0830DA4E-3EB6-4342-A3F3-33E99E19D005","diskIdentifier":"5F2...
```

Arq.app v8 은 backuprecord 를 **UTF-8 JSON 한 줄** (BOM 없음) 로 기록.
`plistlib.loads` 가 즉시 `InvalidFileException` 으로 실패.

Fix:
- **reader** (`arq_reader/restore.py`): 새 `_parse_backuprecord(plain)` 헬퍼
  — plist 시도 후 fail 시 `json.loads(plain.decode("utf-8"))`. 두 형식 모두
  허용해 우리가 만든 (legacy) plist 백업과 Arq.app JSON 백업 모두 read.
- **writer** (`arq_writer/backuprecord.py`): `serialize_backuprecord(fmt='json')`
  를 새 default 로 설정. `fmt='binary-plist'` 는 하위 호환을 위해 유지.

## 3. BlobLoc 바이너리 레이아웃에 `isLargePack` 필드가 빠져 있었음

### Before

우리 `parse_blobloc` 와 `write_blobloc` 의 필드 순서:

```python
# 우리 reader
def parse_blobloc(reader):
    blob_id    = reader.read_string()
    is_packed  = reader.read_bool()       # 7바이트 후 다음 필드
    rel_path   = reader.read_string()
    ...
```

자체 round-trip 에서는 양방향 동일해 통과.

### After (commit `399480a`)

운영자 destination 의 첫 tree blob 을 hex dump 해서 확인:

```
... 31 31 62 | 01 | 00 | 01 00 00 00 00 00 00 00 5a 2f 45 31 42 44 ...
              ^    ^    ^                                    ^
              |    |    isNotNull=1                          rel_path 시작 "/E1BD..."
              |    is_large_pack=False  ← 우리가 빠뜨린 바이트
              is_packed=True
```

실제 Arq.app 의 BlobLoc 레이아웃:
```
blob_id, isPacked, isLargePack, rel_path, offset, length, stretch, compression
                  ^^^^^^^^^^^
                  spec 에 누락; 실 데이터에서 발견
```

이 한 바이트 누락 때문에 모든 다음 필드가 1바이트씩 어긋나 다음 `read_string`
에서 `bad [String] isNotNull byte: 45 (= '-')` 폭발 — UUID 의 하이픈을
isNotNull 바이트로 잘못 해석.

Fix:
- `arq_writer/types.py`: `BlobLoc.isLargePack: bool = False` 필드 추가.
- `arq_reader/parse.py:parse_blobloc`: `read_bool()` 한 번 더.
- `arq_writer/serialize.py:write_blobloc`: `write_bool(loc.isLargePack)` 추가.
- `arq_writer/backuprecord.py:blobloc_to_dict`: JSON 에도 `"isLargePack"` 키 emit.
- `arq_reader/restore.py:_blobloc_from_dict`: JSON 의 `isLargePack` 읽음.

테스트 `test_blobloc_keys_overlap` 으로 lock-in.

## 4. Node 직렬화에 `userName` / `groupName` 필드가 빠져 있었음

### Before

`node_to_dict` 가 emit 하는 키:
```python
{
  "isTree", "computerOSType", "containedFilesCount", "itemSize",
  "modificationTime_sec", "modificationTime_nsec",
  "changeTime_sec", "changeTime_nsec",
  "creationTime_sec", "creationTime_nsec",
  "deleted",
  "mac_st_dev", "mac_st_ino", "mac_st_mode", "mac_st_nlink",
  "mac_st_uid", "mac_st_gid", "mac_st_rdev", "mac_st_flags",
  "winAttrs",
  "treeBlobLoc"/"dataBlobLocs", "xattrsBlobLocs",
}
```

### After (commit `399480a`)

운영자 record 의 `node` dict keys:
```
{... 위와 같음 ..., 'groupName', 'userName', 'reparseTag',
 'reparsePointIsDirectory', ...}
```

`userName`/`groupName` 누락. Numeric uid/gid 만으로는 Arq.app 의 GUI 복원에서
ownership 표시가 안 되거나, `arq_restore` 의 일부 코드 경로가 fail 할 수 있음.

Fix:
- `arq_writer/backuprecord.py:node_to_dict`: 두 키 추가 (값은 `node.username
  or ""`, `node.groupName or ""`).
- `arq_writer/backup.py`: 새 `_resolve_owner(uid, gid)` 헬퍼 — POSIX
  `pwd`/`grp` 모듈로 uid → username, gid → groupname 변환. 실패 (LDAP-only
  환경, Windows) 면 `None` 반환 → writer 가 빈 문자열로 emit.
- `Backup._walk_file` / `_walk_symlink` 의 두 FileNode 생성 지점에서
  `_resolve_owner` 호출.

테스트 `test_node_keys_overlap_with_arq_app` 으로 lock-in.

## 5. SFTP partial-read 가 packed blob 마다 전체 pack 다운로드

### Before

Hetzner 같은 chrooted 서버에서는 `ssh ... -- head -c <length>` 가 거부되므로
`read_range` 의 fallback 이 sftp `get` 으로 **전체 파일 다운로드**:

```python
def _read_range_via_sftp_get(self, path, offset, length, *, timeout):
    fd, tmp = tempfile.mkstemp(...)
    self._run_sftp_batch(f"get {path} {tmp}\nbye\n")
    with open(tmp, "rb") as f:
        f.seek(offset)
        return f.read(length)
```

문제: pack 파일 한 개에 수십 개 blob 이 들어있는데, restore 마다 각 blob 의
range read 가 **같은 pack 파일을 다시 다운로드**. 결과:
- Pack 50MB × N reads = 수백 MB 중복 다운로드
- restore 1회 = 30분+ hang
- 단순한 test 도 끝까지 가지 않음

### After (commit `399480a`)

Per-session pack file 캐시 추가:

```python
class SftpBackend:
    def __init__(self, ...):
        self._read_cache: Dict[str, Path] = {}   # 새 필드

    def _read_range_via_sftp_get(self, path, offset, length, *, timeout):
        cached = self._read_cache.get(path)
        if cached is None or not cached.is_file():
            fd, tmp = tempfile.mkstemp(prefix="arq-sftp-cache-")
            os.close(fd)
            cached = Path(tmp)
            self._run_sftp_batch(f"get {path} {cached}\nbye\n", timeout=timeout)
            self._read_cache[path] = cached
        with open(cached, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def _cleanup(self):
        for cached in list(self._read_cache.values()):
            try: cached.unlink()
            except OSError: pass
        self._read_cache.clear()
```

같은 pack 파일에 대한 N 번째 read 는 local seek (마이크로초). `__exit__` /
`close` 에서 모든 캐시 파일 정리 — 디스크 leak 없음.

검증: writer round-trip 8분 (pre-cache 30분+ → post-cache 8분 — 60-70%
감소).

## 6. List_backuprecords bucket 공식의 잘못된 가정

### Before (commit `7ded492`)

기존 docstring:
```python
def list_backuprecords(...):
    """...
    The lexicographic ordering on (bucket, num) matches chronological
    ordering because both encode creation_date (bucket = floor(creation_date /
    100000), num = creation_date % 100000).
    """
```

테스트가 실 destination 에서 `bucket=176, creationDate=1761965143` 을 발견:
- `floor(1761965143 / 100000) = 17619` ≠ 176
- 실측 비율은 **`/ 10_000_000`** 에 가까움 (176.1965...)
- `num=2351653` 도 `creationDate % anything` 로 설명 안 됨 — 별도 sequence

### After

Spec 의 bucket 공식은 Arq.app 내부 implementation detail 로 격하. Caller 는
**chronological 정렬** 만 의존하므로 그것만 검증:

- docstring: "Arq.app picks (bucket, num) such that lexicographic ordering
  matches creationDate order" 로 변경. 정확한 공식 claim 제거.
- 새 테스트 `test_record_paths_sort_chronologically`: 5폴더 × 5records
  decrypt 후 path 정렬과 creationDate 정렬 일치 검증 (실 destination 에서 PASS).

## 영향도 매트릭스

| 영역 | Before | After | 영향받은 코드 |
|---|---|---|---|
| Hetzner-style SFTP 호환성 | ❌ 모든 backend op 실패 | ✅ sftp 프로토콜만으로 동작 | `arq_validator/sftp.py` |
| Backuprecord 직렬화 | binary plist (not Arq.app) | JSON default + plist back-compat | `arq_writer/backuprecord.py`, `arq_reader/restore.py` |
| BlobLoc 바이너리 레이아웃 | spec 따름 (실제와 다름) | `isLargePack` 추가, Arq.app 일치 | `arq_writer/serialize.py`, `arq_reader/parse.py`, dict 변환기들 |
| Node 직렬화 | uid/gid 만 | userName/groupName 까지 | `arq_writer/backup.py`, `arq_writer/backuprecord.py` |
| SFTP partial-read 성능 | pack 마다 전체 재다운로드 | 세션 캐시 — local seek | `arq_validator/sftp.py` |
| Bucket 공식 docs | 잘못된 100x 차이 | 공식 자체를 contract 에서 제거 | `arq_validator/layout.py` |

## 신규 통합 테스트 카탈로그

`tests/integration/test_arq_real_destination_deep.py` (신규):

| 테스트 클래스 | 테스트 | 검증 내용 |
|---|---|---|
| `FolderAndHistoryParseTests` | `test_every_folder_has_decryptable_latest_record` | 5폴더 모두 최신 record JSON parse |
| | `test_oldest_record_still_decrypts` | 가장 오래된 record 도 decrypt — keyset rotation 무결성 |
| | `test_record_paths_sort_chronologically` | path 정렬 == creationDate 정렬 invariant |
| `TreeBinaryParseTests` | `test_top_level_tree_parses` | 모든 폴더의 root tree blob binary parse |
| | `test_nested_trees_parse_cleanly` | 첫 폴더의 50개 nested tree (`isLargePack` regression test) |
| `WriterFormatCompatTests` | `test_node_keys_overlap_with_arq_app` | 우리 `node_to_dict` 가 Arq.app 의 모든 node 키 emit |
| | `test_record_top_level_keys_overlap` | 우리 `build_backuprecord_dict` 가 Arq.app 의 모든 top-level 키 emit |
| | `test_blobloc_keys_overlap` | 우리 `blobloc_to_dict` 가 Arq.app 의 모든 BlobLoc 키 emit (isLargePack regression) |

`tests/integration/test_arq_real_destination.py` (PR #16):

| 테스트 | 검증 내용 |
|---|---|
| `test_restore_latest_record_of_first_folder` | reader 가 운영자 실 데이터 복원 |
| `test_audit_drip_capped_at_a_few_megabytes` | validator L2 audit-drip on real data |
| `test_round_trip_via_real_sftp` | writer → reader → validator end-to-end (sandbox) |

`tests/integration/test_arqapp_sftp_compat.py` (PR #9, attribute 이름 수정됨):

| 테스트 | 검증 내용 |
|---|---|
| `test_layout_discovers_computer` | discover_layout(enumerate_objects=False) — 빠른 UUID 발견 |
| `test_keyset_decrypts` | 운영자 keyset PBKDF2-SHA256 + AES-CBC 복호화 |
| `test_compatibility_audit_passes` | 25개 invariant 모두 통과 |
| `test_validator_l0_l1a_l1b_tiers_pass` | DEEP tier 통과 |
| `test_fingerprint_is_well_formed_json` | shape fingerprint JSON 직렬화 |
| `test_records_list_at_least_one` | 폴더에 최소 1 record 존재 |
| `test_sample_standalone_object_arqo_valid` | standalone object ARQO + HMAC + blob_id 검증 |

## 핵심 교훈

1. **합성 테스트는 자기 일관성만 보증**한다. 양방향 (reader+writer) 모두 통과해도
   진짜 호환성은 외부 reference 와 비교해야만 검증된다.
2. **Spec 문서가 실제와 다를 수 있다.** Arq 7 공식 spec 의 "binary plist"
   표현은 실제로는 JSON 으로 emit 된다. `isLargePack` 도 spec 에 없다.
3. **SFTP-only 서버는 일반적이다.** Hetzner 외에도 클라우드 storage box 류는
   대부분 chrooted 로 SSH 명령 차단. backend 는 sftp 프로토콜만으로 동작해야
   한다.
4. **운영자가 자기 destination 으로 검증할 수 있는 framework** (`.secrets/` +
   integration tests) 가 sandbox 에서 발견 못 하는 호환성 버그를 즉시 노출시킨다.

## 운영자 가이드

운영자가 자신의 destination 으로 위 테스트를 실행하는 절차는
`docs/COMPAT-SFTP-TESTING.md` 의 "0. 자격증명 소스" 섹션을 참조하세요.
요약:

```sh
cd /path/to/arq-backup-tui
git checkout claude/secrets-real-destination-tests
cp .secrets/sftp.json.example .secrets/sftp.json
cp .secrets/dest_password.example .secrets/dest_password
chmod 600 .secrets/sftp.json .secrets/dest_password
$EDITOR .secrets/sftp.json .secrets/dest_password

python3 -m unittest tests.integration -v   # 전체 통합 테스트
```

자격증명이 비어 있으면 모든 통합 테스트는 자동 skip — 일반 회귀에 영향 없음.
