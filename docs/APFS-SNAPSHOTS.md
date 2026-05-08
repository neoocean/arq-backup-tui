# macOS APFS 스냅샷 기반 백업

## 1. 동기

라이브 macOS 소스 트리를 그대로 walk하며 백업하면 walk 도중
파일 내용이 바뀔 수 있습니다 — 사용자가 hashing 중인 문서를 저장
하면 결과는 pre-save 도 post-save 도 아닌 chunk를 만들어냅니다.
Apple의 권장 해결책은 **APFS 스냅샷**을 먼저 만들고 그 read-only
view를 walk하는 것이며, **Time Machine 자체가 정확히 그렇게
작동**합니다. Arq.app도 같은 패턴을 사용합니다.

본 프로젝트는 macOS에서 옵션으로 같은 보호를 제공합니다.

## 2. 검토한 옵션

### 2.1 macOS 옵션

| 메커니즘 | 도구 | 특이사항 |
|---------|------|---------|
| **Time Machine local snapshot** | `tmutil localsnapshot` | 이름은 `com.apple.TimeMachine.YYYY-MM-DD-HHMMSS.local`; 24시간 후 자동 삭제됨; 누구나 만들 수 있음(sudo 필요) |
| **APFS native snapshot** | `fs_snapshot_create` syscall | 일반적으로 SIP 면제 또는 entitlement가 필요해 일반 도구에서 사용 어려움 |
| **`mount_apfs -s <name>`** | mount tool | 어떤 snapshot이든 read-only로 마운트 가능; sudo 필요 |
| **`tmutil deletelocalsnapshots`** | tmutil | TM snapshot 삭제; sudo 필요 |

**선택**: Time Machine local snapshot + mount_apfs.
- 비특권 entitlement 불필요
- Apple이 공식적으로 지원하는 패턴
- Arq.app도 같은 방식 추정

### 2.2 다른 OS

- **Linux ext4**: 네이티브 스냅샷 없음. LVM이나 btrfs로 우회.
- **Linux btrfs**: `btrfs subvolume snapshot` 가능하지만 본 프로젝트의 macOS 우선 stance와 별개로 다뤄야 함 (이 PR 범위 외).
- **Windows NTFS**: VSS (Volume Shadow Copy) 가능; 별도 PR.

본 PR은 **macOS 만** 지원하며, 다른 OS에서는 라이브 트리 walk로
fallback (옵션 무시).

## 3. 구현 개요

새 모듈: `arq_writer.macos_snapshot`

### 3.1 함수 카탈로그

```python
from arq_writer import (
    is_macos, is_macos_apfs,
    create_snapshot, list_snapshots, delete_snapshot,
    mount_snapshot, unmount_snapshot,
    with_apfs_snapshot,
    SnapshotInfo, SnapshotError, NotMacOSError,
)
```

| 함수 | 권한 필요 | 동작 |
|------|----------|------|
| `is_macos()` | 없음 | bool |
| `is_macos_apfs(path)` | 없음 | bool (diskutil info -plist 호출) |
| `create_snapshot()` | sudo (tmutil) | sudo tmutil localsnapshot → 새 SnapshotInfo |
| `list_snapshots(volume="/")` | 없음 | tmutil listlocalsnapshots → List[SnapshotInfo] |
| `delete_snapshot(snap)` | sudo | sudo tmutil deletelocalsnapshots <date-stamp> |
| `mount_snapshot(snap, mount_point)` | sudo | sudo mount_apfs -s … -o ro,nobrowse |
| `unmount_snapshot(mount_point)` | sudo | sudo umount; 이미-언마운트는 OK |
| `with_apfs_snapshot(source)` | sudo | context manager: create + mount + cleanup |

### 3.2 build_backup 통합

`build_backup`에 옵션 추가:

```python
build_backup(
    source, dest, password,
    use_apfs_snapshot=True,    # 옵션
)
```

흐름:

1. macOS + APFS이면 `tmutil localsnapshot` 실행
2. 임시 mount point에 `mount_apfs -s … -o ro,nobrowse` 마운트
3. 원본 source path를 mount-relative path로 translate
4. 그 frozen view로 walk + 백업
5. cleanup (umount; snapshot은 default로 보존 — TM이 의존)

비macOS이면:
- `apfs_snapshot_skipped` 콜백 이벤트 emit
- 라이브 source로 fallback (실패 없음)

### 3.3 권한 정책

`tmutil localsnapshot` / `tmutil deletelocalsnapshots` /
`mount_apfs` 모두 root가 필요합니다. 라이브러리는 `sudo`를
**호출**하지만 비밀번호 prompt는 OS의 sudo가 처리합니다.

cron / launchd 자동화 사용 시 운영자가 `sudoers`에서 다음을
NOPASSWD로 설정 권장:

```
yourusername ALL=(root) NOPASSWD: /usr/bin/tmutil localsnapshot
yourusername ALL=(root) NOPASSWD: /usr/bin/tmutil deletelocalsnapshots *
yourusername ALL=(root) NOPASSWD: /sbin/mount_apfs *
yourusername ALL=(root) NOPASSWD: /sbin/umount *
```

## 4. 보장 + 한계

### 4.1 보장

- **walk 시작 시점 일관성**: snapshot 마운트 후의 모든 read는 동일 timestamp
- **chunker 안정성**: 같은 source가 같은 chunker로 두 번 백업되면 동일 chunks (live walk에서는 user의 mid-walk save로 인해 미세하게 다를 수 있음)
- **mtime 정합성**: snapshot의 stat이 정확함; live walk는 stat 후 read 사이에 변경 가능

### 4.2 한계

- **macOS only**: Linux btrfs/LVM, Windows VSS는 별도 PR
- **boot 볼륨 한정**: snapshot은 한 APFS 볼륨 전체. 여러 볼륨 병행 백업 시 각각 snapshot
- **sudo prompt**: NOPASSWD 설정이 없으면 매 백업마다 prompt
- **Sandbox 검증 불가**: macOS 없이는 mount_apfs 통합 테스트 불가능; sandbox는 mock 기반 단위 테스트만

### 4.3 안전성

- snapshot 자체는 디스크 공간을 거의 차지하지 않음 (CoW 기반). 디스크 가득 차면 자동 삭제.
- snapshot mount의 cleanup은 `with_apfs_snapshot` context manager가 보장 (예외 발생해도 unmount 시도)
- snapshot **삭제는 default로 안 함** — Time Machine이 자체적으로 그 snapshot을 사용할 수 있음

## 5. Operator 검증 절차 (sandbox에서 테스트 불가능한 부분)

Sandbox에서 검증할 수 있는 것:
- ✅ tmutil 출력 파싱 (mocked)
- ✅ mount_apfs 인자 시퀀스 (mocked)
- ✅ snapshot path 변환 (`/Users/me/foo` → `<mount>/Users/me/foo`)
- ✅ Linux fallback 동작 (`apfs_snapshot_skipped` 이벤트)

Sandbox에서 **검증 불가능한** 것 (macOS 운영자가 paste):

### 5.1 실제 mount + walk 검증

운영자가 macOS에서:

```bash
# 1) 알려진 fixture
mkdir -p /tmp/snap-test
echo hello > /tmp/snap-test/a.txt

# 2) 백업 (snapshot 모드)
arq-backup create /tmp/snap-test \
    --dest /tmp/dest-snap \
    --password test \
    --use-apfs-snapshot

# 3) 검증 — 결과 fingerprint 추출
arq-fingerprint compute /tmp/dest-snap \
    --password test \
    --out /tmp/fp-snap.json

# 4) 같은 source를 라이브 모드로
arq-backup create /tmp/snap-test \
    --dest /tmp/dest-live \
    --password test
arq-fingerprint compute /tmp/dest-live \
    --password test \
    --out /tmp/fp-live.json

# 5) 비교 — 같아야 함 (file 내용 동일하므로)
arq-fingerprint compare /tmp/fp-snap.json /tmp/fp-live.json
```

`arq-fingerprint compare`이 `match: true`로 출력하면 snapshot
경로가 정상 동작.

### 5.2 mid-walk 변경 stress

운영자가:

```bash
# 1) 큰 fixture
mkdir -p /tmp/big-test
for i in {1..1000}; do head -c 10240 </dev/urandom > /tmp/big-test/f$i.bin; done

# 2) 동시에:
#    - 터미널 A: arq-backup create /tmp/big-test --dest /tmp/dest --use-apfs-snapshot
#    - 터미널 B: while true; do echo extra >> /tmp/big-test/f1.bin; done

# 3) 백업 끝나면 restore
arq-backup의 결과로 만들어진 destination을 restore
diff /tmp/big-test/f1.bin /restored/f1.bin   # 어떤 한 시점이어야 함
```

snapshot 모드면 `f1.bin`이 backup 시작 시점의 버전으로 재구성되어
diff가 빈 출력 (또는 backup 실행 중간 어디든 한 시점) 이어야
합니다 — torn write가 없어야 함.

라이브 모드는 동일한 stress 테스트에서 `f1.bin`이 부분적으로
구버전 + 신버전이 섞여 무효한 결과가 될 수 있음.

## 6. 향후 작업

본 PR은 macOS만 다룹니다. 동일 패턴을 다른 OS로 확장:

| OS | 메커니즘 | 노력 |
|----|---------|------|
| Linux btrfs | `btrfs subvolume snapshot` | 작음 (~50 LOC) |
| Linux LVM | `lvm lvcreate --snapshot` | 작음 |
| Linux ext4 (snapshot 없음) | unsupported; fallback only | N/A |
| Windows VSS | `wmic shadowcopy create` 또는 PowerShell | 중간 (~150 LOC) |

각각 별도 PR로 추가 가능. 현재는 macOS만 우선순위.

## 7. CLI 노출

이 PR은 `build_backup(use_apfs_snapshot=True)` Python API + 옵션
인자로 노출합니다. CLI (`arq-backup`) 의 `--use-apfs-snapshot`
플래그는 별도 follow-up PR에서 추가하는 것이 안전합니다 (CLI는
이미 안정 surface이므로 한 PR에 너무 많이 묶지 않기 위해).
