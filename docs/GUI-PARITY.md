# Arq 7 GUI 기능 패리티

이 문서는 **Arq 7 데스크톱 앱(GUI)** 기능 전체를 catalog하고
현재 코드베이스의 구현 상태를 표시합니다. 미구현 항목 중에서
프로젝트 스코프 안에 들어가는 것들은 우선순위와 구현 노트를
함께 기재합니다.

스코프 결정사항 (이전 결정 누적):

- **로컬 / NAS / SFTP 스토리지만**: 클라우드 백엔드 (S3 / Wasabi
  / B2 / Dropbox / OneDrive / Box / GCS / Azure / Google Drive /
  pCloud …) 모두 🔴 **out of scope**
- **백업 호스트**: 정책 레이어 기능 (스케줄 / throttling /
  notifications / 메뉴바 / wake-from-sleep)은 🔴 **out of scope**
- **TUI English-only**, 백업된 파일/경로명은 unicode-safe
- **Plan editing UI**는 v1.x로 deferred (recreate 권장)
- **xattrs / ACLs / resource forks / Finder metadata**: cross-
  platform stance로 🔴 out of scope

## 1. Headline 패리티 표

| 영역 | Arq 7 GUI 기능 | 현재 상태 | 비고 |
|------|----------------|:--------:|------|
| 백업 플랜 | 생성 | ✅ | TUI Wizard (M3) |
| 백업 플랜 | 편집 | ❌ | v1.x deferred (recreate 권장) |
| 백업 플랜 | 삭제 | ✅ | `arq-tui plans delete <id-or-name>` |
| 백업 플랜 | 목록 | ✅ | TUI Home + `PlanRegistry.list_plans` |
| 백업 플랜 | 복수 플랜 | ✅ | 한 컴퓨터에 multiple plans |
| 소스 선택 | GUI 폴더 picker | ✅ | TUI SourcePicker |
| 소스 선택 | 다중 소스 | ✅ | M3부터 지원 |
| 소스 선택 | 파일 크기 제한 | ✅ | `Backup(max_file_bytes=...)` |
| 소스 선택 | 와일드카드 exclusion | ✅ | `ExclusionRules.of(wildcard=...)` |
| 소스 선택 | 정규식 exclusion | ✅ | `ExclusionRules.of(regex=...)` |
| 소스 선택 | .gitignore 호환 | ✅ | `ExclusionRules.of(gitignore_lines=...)` 부분구현 |
| 소스 선택 | 드라이브 / WiFi 제외 | 🔴 | 정책 레이어 |
| 백업 실행 | 수동 실행 | ✅ | CLI / TUI |
| 백업 실행 | 스케줄 실행 | 🔴 | 외부 cron 권장 |
| 백업 실행 | 일시정지 / 재개 | ❌ | 향후 (audit-drip 패턴 응용 가능) |
| 백업 실행 | 협조 취소 | ✅ | `Backup.cancel()` |
| 백업 실행 | CPU throttling | 🔴 | 정책 레이어 |
| 백업 실행 | 네트워크 throttling | ⚠️ | audit-drip만 throttle 가능 |
| 백업 실행 | 배터리 인지 | 🔴 | 정책 레이어 |
| 백업 실행 | wake-from-sleep | 🔴 | OS 레벨 |
| 암호화 | 초기 비밀번호 | ✅ | wizard 단계 3 |
| 암호화 | 비밀번호 변경 | ✅ | `arq_writer.rotate_keyset_password(blob, old_pw, new_pw)` |
| 암호화 | 비밀번호 복구 | 🔴 | Arq Cloud 한정 |
| 스토리지 | 로컬 / NAS | ✅ | LocalBackend |
| 스토리지 | SFTP | ✅ | SftpBackend (read+write) |
| 스토리지 | 클라우드 (S3 / B2 / GCS / Azure …) | 🔴 | out of scope; rclone mount 우회 |
| 스토리지 | 다중 destination | ❌ | v1.x — 한 plan = 한 destination |
| 청커 | 일반 Buzhash | ✅ | `arq_writer.chunker` |
| 청커 | Arq.app v7.41 매칭 | ✅ | `arq_writer.arq_chunker_params` |
| 청커 | 폴더별 useBuzhash 토글 | ✅ | `Backup.add_folder(..., chunker_config=...)` + `Plan.per_source_chunkers` |
| 객체 저장 | standardobjects/ | ✅ | 기본 모드 |
| 객체 저장 | treepacks / blobpacks | ✅ | `use_packs=True` |
| 객체 저장 | largeblobpacks 읽기 | ✅ | 읽기는 됨 |
| 객체 저장 | largeblobpacks **쓰기** | ✅ | `Backup(large_blob_threshold=...)` 자동 라우팅 |
| 복원 | 전체 복원 | ✅ | RestoreRunScreen |
| 복원 | 선택적 경로 복원 | ✅ | `paths=[...]` |
| 복원 | 시점 선택 (historical) | ✅ | `backuprecord_path=...` |
| 복원 | 다른 위치로 복원 | ✅ | `dest=...` |
| 복원 | mtime 보존 | ✅ | `Restore._restore_file_node`가 utime |
| 복원 | mode (perm) 보존 | ✅ | `os.chmod(out_path, S_IMODE(node.mac_st_mode))` |
| 복원 | uid / gid 보존 | ❌ | cross-platform; UI 외부 정책 |
| 복원 | symlink 물리적 생성 | ✅ | writer가 link target 저장 + S_IFLNK; restorer가 `os.symlink` |
| 복원 | 하드링크 감지 | ❌ | Arq.app도 별도 파일로 처리 |
| 복원 | xattr / ACL | 🔴 | cross-platform stance |
| 복원 | 리소스 포크 / Finder | 🔴 | cross-platform stance |
| 복원 | FUSE 마운트 | 🔴 | cross-platform; rclone 권장 |
| 복원 | Quick Look 미리보기 | 🔴 | macOS-only |
| 복원 | 스냅샷 간 diff | ❌ | v1.x |
| 검증 | 수동 검증 | ✅ | 4-tier + audit-drip |
| 검증 | 자동 (월간) 검증 | 🔴 | 외부 cron 권장 |
| 검증 | 형식 conformance | ✅ | `check_arq7_compatibility` |
| 모니터링 | 활동 로그 | ⚠️ | callback events 노출, GUI viewer 없음 |
| 모니터링 | 이메일 보고서 | 🔴 | 정책 레이어 |
| 모니터링 | 시스템 알림 | 🔴 | OS-specific |
| 모니터링 | 메뉴바 / 시스템 트레이 | 🔴 | OS-specific |
| 보존 | hourly / daily / monthly 정책 | ❌ | v1.x — 본 PR에서 일부 |
| 보존 | 오래된 commit 수동 삭제 | ❌ | v1.x |
| 보존 | blob GC / vacuum | ❌ | v1.x |
| 다중 컴퓨터 | 한 destination 공유 | ⚠️ | reader 자동 발견; writer는 single |
| 다중 컴퓨터 | 컴퓨터별 keyset | ✅ | 각 `<CU>/encryptedkeyset.dat` 독립 |
| 내보내기 / 가져오기 | plan 설정 export | ❌ | v1.x — `~/.config/arq-backup-tui/plans/<id>.json` 그대로 복사로 우회 |
| 내보내기 / 가져오기 | plan 설정 import | ⚠️ | 같은 위에 같은 우회 |

## 2. 우선순위 + 구현 계획

본 PR에서 **구현**하는 항목 (모두 in-scope, 작거나 중간 규모):

### Phase 1 — 복원 메타데이터 (작음, 높은 가치)

1. **mode (perm bits) 복원** — `Restore._restore_file_node`에 `os.chmod(out_path, mode & 0o7777)` 추가. 이미 mtime은 처리됨.
2. **Symlink 처리** — writer가 `Path.is_symlink()`을 감지해서:
   - 링크 타겟 문자열을 plaintext로 저장
   - FileNode mode bits에 `stat.S_IFLNK` 포함
   - 추가 boolean `is_symlink` 메타 (또는 mode bit으로 판별)
   restorer가 mode bits에서 S_IFLNK를 보면 `os.symlink(target, path)`를 호출

### Phase 2 — 소스 필터링 (중간)

3. **파일 크기 제한** — `Backup` / `build_backup`에 `max_file_bytes: Optional[int] = None`. `_walk_file`에서 `src.stat().st_size > max`이면 skip + emit `file_skipped` 콜백.
4. **Exclusion 패턴** — `Backup`에 `exclusions: ExclusionRules` 파라미터:
   - **glob** (`*.log`, `node_modules/`)
   - **정규식**
   - **`.gitignore` style** (선택적, gitignore-parser-like)
   `_walk_dir`에서 entry name 또는 rel_path 매칭 시 skip.

### Phase 3 — 스토리지 정교화 (작음)

5. **`largeblobpacks/` 라우팅** — `_write_blob`에서 `len(arqo) > maxPackedItemLength` (~256 KiB)이고 `use_packs=True`이면 `_large_blob_pack`으로 라우팅. 새 PackBuilder 인스턴스.
6. **폴더별 useBuzhash 토글** — `Plan.folder_chunkers: Dict[folder_name, str]` 또는 wizard 단계에 폴더별 청커 선택. `add_folder` 호출 시 인자로 `chunker_config` override.

### Phase 4 — Plan / keyset 관리 (중간)

7. **CLI plan 명령어** — `arq-backup plans list / show <id> / delete <id>` 추가. 삭제 시 인지된 destination에서 메타파일 삭제는 위험하므로 plan json 파일만 삭제.
8. **비밀번호 변경** — `arq_writer.crypto_write.rotate_keyset_password(dest_root, computer_uuid, old_password, new_password)`. 절차:
   - 기존 keyset 복호 → (encryption_key, hmac_key, blob_id_salt) 보존
   - 새 salt + IV 생성
   - 같은 키들을 새 password로 다시 암호화
   - 기존 backuprecord / blob들은 변경 없음 (그것들의 hmac_key / encryption_key는 동일)

### Phase 5 — 보존 (가장 큰 작업; 본 PR에서는 **계획만**, 구현은 follow-up)

9. **Retention 정책 + blob GC**: 다음 단계로 분리.

## 3. 본 PR 출력물

각 phase별 commit + 통합 테스트 + COVERAGE.md / MECHANISM.md
업데이트.

미구현 항목 중 v1.x로 미루는 것은 본 문서의 표에 명시된 그대로
유지. 향후 별도 PR에서 처리.
