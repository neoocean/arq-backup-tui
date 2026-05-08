# 다국어 / 유니코드 / 이모지 / 긴 경로 처리 보증

이 문서는 본 프로젝트가 **파일 이름 / 경로 이름** 에 들어갈 수
있는 다양한 문자열을 어떻게 보존하고 복원하는지에 대한 계약을
정의합니다. 모든 항목은 자동화된 테스트(`tests/
test_unicode_path_stress.py`, `tests/fixtures_unicode.py`)로
검증됩니다.

## 1. 핵심 보증

### 1.1 byte-level round-trip

소스 파일시스템에서 한 파일을 백업한 뒤 복원하면, 다음 두 값이
**byte-for-byte 동일**합니다:

| 비교 대상 | 보증 |
|----------|------|
| `src.read_bytes()` ↔ `restored.read_bytes()` | ✅ |
| 소스 트리에서 파일에 도달하는 component name 시퀀스 (UTF-8 bytes) | ✅ |

이 보증은 사용된 문자(언어 / script / 정규화 형식 / 이모지 /
ZWJ 시퀀스 / 결합 문자) 와 무관합니다.

### 1.2 적용 범위

| 위치 | 처리 방식 |
|------|----------|
| Tree blob의 child name | `[String]` 형식: `[is_not_null:1B] + [length:UInt64 BE] + UTF-8 bytes` (spec convention 그대로) |
| `backupplan.json` / `backupfolder.json` / `backupconfig.json` / `backupfolders.json` | `json.dumps(..., ensure_ascii=False).encode("utf-8")` — 비ASCII 문자가 `\uXXXX` 이스케이프 되지 않고 native UTF-8로 저장됨 |
| `backuprecord` (binary plist) | `plistlib`이 자동으로 UTF-8 string으로 처리 |
| Symlink 타겟 | `os.readlink(src).encode("utf-8")` 로 plaintext 저장 → `os.symlink(target_str, dest)` 로 복원 |
| TUI 표시 라벨 | Textual + Rich가 BMP 외 문자(이모지 등)를 정확히 렌더링 |

### 1.3 정규화 정책: **변경 없음**

본 라이브러리는 NFC / NFD / NFKC / NFKD **어떤 형식으로도 정규화하지 않습니다**.

- 소스에 NFC `한글` (precomposed)이 있으면 destination에도 그대로
  precomposed bytes로 저장됨.
- 소스에 NFD `한+ㄱ` (decomposed jamo) 형식이 있으면 그대로
  decomposed bytes로 저장됨.
- 결합 문자 / variation selector / ZWJ 시퀀스 보존됨.

**주의**: macOS HFS+ (legacy)는 파일시스템 레벨에서 NFD로
강제 정규화합니다. 이런 경우 source filesystem이 이미 NFD로
바꾼 결과를 그대로 받아 저장합니다 — 우리 코드는 정상 동작이지만
"같은 글자"라는 시각적 의미가 보존되지 않을 수 있습니다.
APFS (현대 macOS 기본) 와 ext4 / btrfs / xfs (Linux) 는 byte-
preserving 이므로 정확히 round-trip 됩니다.

## 2. 검증된 시나리오

`tests/fixtures_unicode.py` 의 fixture generator가 만들어내는
fixture들은 모두 backup → format conformance audit → restore
의 전체 파이프라인을 통과합니다:

### 2.1 다국어 (multi-script)

| Script | 예시 |
|--------|------|
| Latin extended | `café_naïveté.txt`, `résumé.pdf`, `fiancée_façade.txt` |
| Hangul (한글) | `한글_파일.txt`, `문서/이력서.txt`, `사진/가족사진.jpg` |
| 일본어 (Hiragana + Katakana + Kanji) | `日本語ファイル.txt`, `写真/東京タワー.jpg`, `ひらがな.txt`, `カタカナ.txt` |
| 中文 (간체 + 번체) | `简体中文.txt`, `繁體中文.txt`, `文件夹/数据.csv` |
| Arabic (RTL) | `مرحبا.txt`, `مجلد/ملف.txt` |
| Hebrew (RTL) | `שלום.txt`, `תיקייה/קובץ.txt` |
| Greek | `ελληνικά.txt` |
| Cyrillic | `русский.txt`, `Москва/Кремль.png` |
| Thai | `ไทย.txt`, `เอกสาร/บันทึก.txt` |
| Devanagari (Hindi) | `हिन्दी.txt` |
| 베트남어 (tone marks) | `tiếng_việt.txt`, `Hà_Nội.png` |

### 2.2 이모지 + ZWJ 시퀀스

| 종류 | 예시 |
|------|------|
| 단일 codepoint emoji | `🎵_song.mp3`, `📁folder`, `🚀_launch.txt` |
| Variation selector (`U+FE0F`) | `❤️.txt` |
| Country flag (Regional Indicator pair) | `🇰🇷.txt` |
| Skin-tone modifier | `👋🏽.txt` |
| **ZWJ 시퀀스 (4명 가족)** | `👨‍👩‍👧‍👦_family.png` |
| Surrogate-pair-heavy | `🌍_world.json` |

특히 ZWJ 시퀀스 (`U+200D`로 결합된 7-codepoint 가족 이모지)는
어떤 layer에서도 normalize / strip 하지 않고 raw bytes로
보존됩니다.

### 2.3 특수 문자 (POSIX-legal)

| 카테고리 | 예시 |
|---------|------|
| 공백 | `with spaces.txt`, `  leading-space.txt`, `trailing-space  .txt` |
| 다중 점 | `...dotted....txt`, `tar.gz.bz2.xz` |
| 대시 / 언더스코어 | `-leading-dash.txt`, `--double-dash.txt` |
| 괄호 | `(parens).txt`, `[brackets].txt`, `{braces}.txt`, `<angle>.txt` |
| 인용부호 | `quote'single.txt`, `quote"double.txt` |
| 이스케이프 문자 | `back\slash.txt`, `tab\there.txt` |
| 구분자 | `comma,sep.txt`, `semi;col.txt`, `amp&rsand.txt`, `pipe\|symbol.txt` |
| Wildcard 문자 | `qmark?.txt`, `star*.txt`, `colon:test.txt` |
| 통화 / 수학 기호 | `$dollar.txt`, `€euro.txt`, `¥yen.txt` |

### 2.4 정규화 형식

NFC vs NFD 두 형식 모두 raw bytes로 저장 + 복원되며 host
파일시스템이 어느 한쪽으로 collapse하지 않는 한 같은 디렉터리
안에 공존 가능:

- `español` (NFC: `e + s + p + a + ñ(precomposed)`)
- `español_nfd` (NFD: `e + s + p + a + ñ(n + combining tilde)`)
- `한글` (Hangul precomposed)
- `한글_jamo` (Hangul jamo decomposed)

### 2.5 긴 이름

| 종류 | 길이 | 예시 |
|------|------|------|
| ASCII 250자 + ".txt" | 254 bytes | `aaa…a.txt` |
| 한글 80자 + ".txt" | 244 bytes (UTF-8 3 byte/char) | `한한한…한.txt` |
| 이모지 60개 + ".txt" | 244 bytes (UTF-8 4 byte/char) | `🎵🎵…🎵.txt` |

**Linux ext4 / btrfs / xfs / APFS / NTFS 의 NAME_MAX = 255 bytes**
한도 안에서 모두 동작 확인.

### 2.6 깊은 디렉터리

`tests/fixtures_unicode.make_deep_path_tree(levels=30)`는 30단계
중첩 디렉터리를 만들어 leaf 파일까지 round-trip 됨을 확인.

총 path 길이가 ~1000 bytes 정도 — Linux PATH_MAX (4096),
macOS PATH_MAX (1024) 양쪽에서 안전.

## 3. OS / 파일시스템 한계 처리

### 3.1 한계 도달 시 동작

본 라이브러리는 OS / 파일시스템 한계 (NAME_MAX, PATH_MAX, 권한)
을 **하드코딩하지 않습니다**. 한계는 host OS / 파일시스템이
강제하며, Python의 `mkdir` / `write_bytes` 호출이 `OSError`로
실패할 때 우리 코드가 catch + emit합니다:

| 실패 위치 | 발생 이벤트 | 동작 |
|----------|----------|------|
| `src.iterdir()` 권한 거부 | `dir_read_error` | 해당 디렉터리만 빈 children으로 처리, 백업 계속 |
| `src.read_bytes()` 권한 거부 | `file_read_error` | 해당 파일을 0 byte로 처리 (FileNode는 emit) |
| `out_path.write_bytes()` 디스크 부족 | `OSError` 그대로 propagate | restore 실패 |
| Source 이름이 너무 길다 | OS가 처음부터 만들지 못함 → walk가 못 봄 | N/A (소스에 없으므로 백업 안 됨) |

즉, **"불가능한 입력은 발생하지 않는다"** 는 가정 위에서 동작.
파일시스템이 이미 거부한 이름은 우리 layer까지 도달하지 않으며,
일부 entry의 read 실패는 전체 백업을 무산시키지 않고 best-effort로
계속됩니다.

### 3.2 OS-specific 알려진 한계

| OS / 파일시스템 | 제한 | 본 라이브러리 동작 |
|----------------|------|-------------------|
| Linux ext4 | NAME_MAX 255 bytes, PATH_MAX 4096 | 한도 내 모두 동작 |
| Linux btrfs | NAME_MAX 255 bytes | 동일 |
| macOS APFS | NAME_MAX 255 bytes, PATH_MAX 1024 (default) | 동일; PATH_MAX 한도 인지 |
| macOS HFS+ | NFD 강제 정규화, `:` 금지 | 입력이 이미 NFD bytes 형태로 들어옴 → 그대로 저장 |
| Windows NTFS | 일부 문자 (`<>:"/\\|?*`) 금지, 예약어 (`con`/`nul`/`aux`) | 본 프로젝트는 POSIX 우선; Windows에서 source가 그런 문자 없음 |
| SFTP server (대부분) | 서버측 파일시스템 한계 따름 | 서버 OSError를 backend layer가 RuntimeError로 surface |

## 4. 구현 키 포인트

### 4.1 JSON sidecar `ensure_ascii=False`

`arq_writer/backup.py`의 4개 JSON sidecar emit 모두
`ensure_ascii=False`를 사용:

```python
self.backend.write_all(
    self._cu_path("backupplan.json"),
    json.dumps(plan, indent=2, ensure_ascii=False).encode("utf-8"),
)
```

이로써:

- **이전 (버그)**: `"name": "\\ud55c\\uad6d\\uc5b4-\\ud3f4\\ub354"` 형식의 escape 발생
- **현재**: `"name": "한국어-폴더"` 그대로 저장

비ASCII 폴더 이름이 plan / config / folder JSON에 들어갈 때
모두 native UTF-8 bytes로 round-trip.

### 4.2 Tree binary [String] 인코딩

`arq_writer/serialize.py:write_string`:

```python
def write_string(value: Optional[str]) -> bytes:
    if value is None:
        return b"\x00"
    encoded = value.encode("utf-8")
    return b"\x01" + struct.pack(">Q", len(encoded)) + encoded
```

길이는 8-byte big-endian, payload는 UTF-8 bytes. 최대 길이는
`2^64 - 1` (사실상 무제한). 어떤 codepoint도 그대로 통과.

### 4.3 Symlink 타겟 string

writer (`arq_writer/backup.py:_walk_file`):

```python
if src.is_symlink():
    data = os.readlink(src).encode("utf-8")
    st = src.lstat()  # symlink 자체의 메타데이터
```

restorer (`arq_reader/restore.py:_restore_file_node`):

```python
if stat.S_ISLNK(int(node.mac_st_mode)):
    target = body.decode("utf-8", errors="replace")
    os.symlink(target, out_path)
```

`errors="replace"` 는 손상된 backup의 안전한 fallback. 정상 backup
에서는 raw UTF-8 그대로 복원됨.

### 4.4 path filter (선택적 복원) byte-level 매칭

`arq_reader/restore.py:_PathFilter`:

```python
def matches(self, rel_path: str) -> bool:
    rel_path = rel_path.strip("/")
    if rel_path in self.keep:
        return True
    for k in self.keep:
        if rel_path.startswith(k + "/"):
            return True
    return False
```

string 비교 (Python 내부 codepoint 시퀀스 비교) → UTF-8 bytes
한 단위 매치. `paths=["한글폴더"]` 가 `한글폴더/메모.txt`에
매칭됨.

## 5. 테스트 커버리지

`tests/test_unicode_path_stress.py` 의 16개 테스트가 본 보증
범위 전체를 cover:

| 클래스 | 무엇을 검증하나 |
|--------|----------------|
| `MultiScriptStressTests` | 11개 script의 25개 파일명 backup → audit → restore (standalone + packed 모드) |
| `EmojiStressTests` | 8개 이모지 / ZWJ 시퀀스 round-trip |
| `SpecialCharsStressTests` | 28개 특수문자 파일명 round-trip |
| `NormalizationStressTests` | NFC + NFD 형식 보존 |
| `LongNameStressTests` | 250+ byte 파일명 (ASCII / 한글) |
| `DeepPathStressTests` | 30단계 nested directory |
| `CombinedStressTests` | 모든 fixture를 한 source 안에서 |
| `JsonSidecarUnicodeTests` | JSON 파일에 `\uXXXX` escape 없음 |
| `CompatibilityOnUnicodeDestinations` | format conformance audit이 multi-script destination 통과 |
| `TreeWalkReuseUnicodeTests` | 한글 / 이모지 경로의 cross-run dedup |
| `PathLengthBoundaryTests` | PATH_MAX 근접 + 권한 거부 graceful handling |

## 6. 한계와 향후 개선

- **invalid UTF-8 byte sequence**: Linux는 `surrogateescape`로
  ext-encoded 비-UTF-8 파일명을 다룰 수 있지만, 본 라이브러리의
  `write_string`은 그런 string을 `UnicodeEncodeError`로 거부.
  실제로는 modern Linux/macOS의 모든 파일시스템이 UTF-8을 강제
  하거나 권장하므로 거의 발생하지 않음.
- **Windows long-path mode (`\\?\` prefix)**: 본 프로젝트는
  POSIX 우선; Windows native long-path 지원은 향후 필요 시 추가.
- **시각적으로 같은 다른 codepoints**: `'A'` (U+0041) vs
  `'А'` (Cyrillic A, U+0410) 처럼 시각적으로 같지만 codepoint가
  다른 경우 → 라이브러리는 raw bytes로 처리하므로 두 파일은 다른
  파일로 인식됨 (Arq.app도 동일).
