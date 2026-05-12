# Weekend human-intervention plan

운영자가 직접 Arq.app GUI나 외부 시스템(network, clock,
filesystem)을 통제해야만 검증 가능한 항목들. 자율 모드에서는
시뮬레이션 또는 사후 fingerprint pin으로만 다룰 수 있고,
**진짜 검증은 운영자가 주말에 진행**해야 함.

각 항목은 다음 5요소로 정리:
1. **무엇을 검증하는지**
2. **왜 자율 불가능한지**
3. **운영자가 해야 할 단계**
4. **성공 / 실패 시그널**
5. **기존 자율 작업과의 관계**

---

## P1 — Strategy I (Arq.app GUI restore of our fresh-walk emit)

### 1. 무엇을 검증하는지
우리 writer가 emit한 신선한 Tree v4 destination을 Arq.app GUI가
복원 가능한가? 복원된 파일 콘텐츠가 source와 byte-identical인가?

### 2. 왜 자율 불가능한지
Arq.app GUI에서 destination을 add → password 입력 → restore
버튼 클릭 → target directory 지정 → restore 완료 확인까지
GUI 인터랙션이 필요. AppleScript/automator로 자동화 가능
하지만 GUI 상태(error dialog, password prompt 등) 처리가
까다로움. 운영자 직접이 가장 빠름.

### 3. 운영자 단계
```bash
# 1. 신선한 v4 destination을 우리 writer로 emit.
python3 -m arq_writer create \
    --dest /Volumes/operator-disk/strategy-i-test \
    --password "$(cat .secrets/strategy-i-pw)" \
    --tree-version 4 \
    --use-packs \
    /tmp/strategy-i-source
```

```
# 2. Arq.app GUI를 열고:
#    Settings → Destinations → "+" → Local Folder
#    → /Volumes/operator-disk/strategy-i-test
#    → password 입력
# 3. Restore 탭 → 위 destination 선택 → 가장 최근 백업 선택
# 4. Restore → target = /tmp/strategy-i-restored
# 5. 복원 완료 후:
diff -r /tmp/strategy-i-source /tmp/strategy-i-restored
```

### 4. 성공 / 실패
- 성공: `diff -r` 출력 비어있음 → **Strategy I 통과** (Tree v4 fresh-walk emit이 Arq.app GUI에 받아들여짐을 정의적으로 증명)
- 실패: Arq.app이 "Backup set corrupt" 또는 "Cannot read backup" 류 에러 → 우리 writer가 GUI reader가 검증하는 어떤 invariant를 위반. 에러 메시지 + Arq.app 로그 (`~/Library/Logs/ArqAgent/`)를 sandbox로 paste

### 5. 기존 자율과의 관계
Strategy I-alt (PR #146, patched arq_restore) + Strategy K (trailing-block RE)이 close-call로 같은 invariant를 검증하지만, GUI는 다른 코드 경로를 사용할 수 있음 (NSURLSession network checks, GUI-only validation passes, etc.).

---

## P2 — K4 first-walk-time correlation

### 1. 무엇을 검증하는지
Arq.app GUI가 신선한 source를 처음 백업했을 때 emit하는
Tree v4 trailing-block bytes 0-15가 정확히 어떤 timestamp인지
(우리 statistical RE는 btime 47.6% + ctime 88.2% 잔여로
설명했지만, "Arq.app's per-Node first-emit-time"이라는 정확한
의미는 미확인).

### 2. 왜 자율 불가능한지
신선한 source는 우리가 만들 수 있지만, 그 source를 **Arq.app GUI로** 백업해야 함 (우리 writer의 emit은 이미 K4-2까지 분석됨). GUI 인터랙션 필요.

### 3. 운영자 단계
```bash
# 1. 절대 백업한 적 없는 새 source 디렉토리 생성
mkdir /tmp/k4-fresh-source
echo "test content alpha" > /tmp/k4-fresh-source/a.txt
echo "test content bravo" > /tmp/k4-fresh-source/b.txt
# 현재 시각 기록
date +%s > /tmp/k4-fresh-source/.test-time-before
```

```
# 2. Arq.app GUI에서:
#    Settings → Backup Plans → "+" → New Plan
#    → source = /tmp/k4-fresh-source
#    → destination = 새로운 또는 기존 destination
#    → 즉시 백업 트리거
```

```bash
# 3. 백업 완료 후 다시 시각 기록
date +%s > /tmp/k4-fresh-source/.test-time-after
# 4. 자율 분석 스크립트로 신선 백업의 trailing-block 추출 + 비교
python3 scripts/k4_subtree_sweep.py \
    --destination /path/to/destination \
    --password-file .secrets/dest_password \
    --records 1 --max-depth 1 > /tmp/k4-fresh-result.txt
```

### 4. 성공 / 실패
trailing_sec ≈ test-time-before (백업 트리거 직전 시각) → "first-walk-time" 가설 확정.
trailing_sec ≈ 파일의 btime/ctime → K4-2 결과 재확인 (가설 약화).
중간값 → 다른 의미.

### 5. 기존 자율과의 관계
K2/K3/K4-1/K4-2의 statistical 분석이 가설을 좁혔지만 정의적
증명은 신선한 GUI 백업 사례 1건만 있으면 끝남.

---

## P3 — Arq.app GUI가 우리 writer의 mixed Arq5/Arq7 destination을 보여주기

### 1. 무엇을 검증하는지
운영자가 이전에 Arq 5/6으로 백업한 destination에 우리 Arq 7
writer가 새 records를 추가하면, Arq.app GUI가 두 종류를 모두
인식해서 보여주는가?

### 2. 왜 자율 불가능한지
GUI의 "backup set 표시" 동작은 우리가 자동화하기 어려움.

### 3. 운영자 단계
```
# 1. 운영자에게 Arq 5/6 백업이 있는 destination 확인.
#    (역사적으로 운영자가 Arq 5/6에서 7으로 마이그레이션
#     했다면 /Volumes/arqbackup1 안의 일부 폴더가 mixed
#     상태일 수 있음.)
```

```bash
# 2. 우리 writer로 그 destination에 Arq 7 record 추가
python3 -m arq_writer create \
    --dest /path/to/mixed-dest \
    --password "$(cat .secrets/dest_password)" \
    --tree-version 4 \
    --dedup-against-existing \
    --computer-uuid <기존 CU> \
    /tmp/mixed-test-source
```

```
# 3. Arq.app GUI 열기 → 그 destination 선택 → backup-set 목록 확인
```

### 4. 성공 / 실패
- 성공: GUI가 우리 Arq 7 record + 기존 Arq 5/6 record 모두 표시
- 실패: GUI가 "format mismatch" 또는 우리 record만 / 기존 record만 표시

### 5. 기존 자율과의 관계
I1 (3-pass idempotent) 검증은 우리 writer의 cross-run 동작만.
Arq 5/6/7 cross-tool은 GUI 검증이 필요.

---

## P4 — Arq.app 데몬 동시성 race (A1, A2, A3)

### 1. 무엇을 검증하는지
- A1: Arq.app daemon이 active한 destination에 우리 writer가 incremental 백업을 추가하면 lock 충돌? race?
- A2: 우리 emit 위에 GUI에서 retention prune 트리거
- A3: GC 부분 실패 후 우리 reader 동작

### 2. 왜 자율 불가능한지
Arq.app 데몬은 PID + lockfile + SQLite의 advisory lock으로
보호됨. 우리가 데몬을 시작 / 중단 / 재현시킬 표준 메커니즘이
없음 (위험할 수도 있음 — 운영자의 실 백업 작업 방해).

### 3. 운영자 단계
```bash
# A1 시나리오:
# 1. 운영자의 실 backup plan을 일시정지 (Arq.app GUI에서)
arqc pauseBackups 60
# 2. 즉시 우리 writer를 같은 destination + 같은 CU로 트리거
python3 -m arq_writer create \
    --dest /path/to/operator-dest \
    --password "..." \
    --dedup-against-existing \
    --computer-uuid <기존 CU> \
    /tmp/concurrency-test
# 3. resume + Arq.app이 우리 record를 어떻게 처리하는지 관찰
arqc resumeBackups
```

```bash
# A2 시나리오 (retention prune):
# 1. 우리 writer로 10개 record를 빠르게 추가
# 2. Arq.app GUI → backup plan settings → retention 정책 강화
# 3. 운영자가 GUI에서 retention 즉시 적용
# 4. 잔존 record 수 확인
```

```bash
# A3 (GC 부분 실패):
# 1. retention prune 트리거
# 2. GC 중 ctrl-C 또는 process kill로 중단
# 3. 우리 reader로 destination 재해석 → 어떤 invariant 위반이 보이는지
```

### 4. 성공 / 실패
- 성공: Arq.app + 우리 writer가 lock 협상을 통해 안전하게 동작; 또는 우리 writer가 "destination locked" 류 명확한 에러를 raise
- 실패: silent corruption (반쪽 emit), 또는 우리 reader가 SQLite hydration 불가능한 상태로 destination을 남김

### 5. 기존 자율과의 관계
F1 (resumable backup), F2 (mid-walk cancel)는 우리 writer 한 process 내부 race만 검증. Arq.app 데몬과의 cross-process race는 미검증.

---

## P5 — Arq.app GUI가 우리 binary-plist BackupRecord 받아들이기

### 1. 무엇을 검증하는지
우리 writer는 default로 JSON, optional binary-plist로 emit. GUI가 binary-plist도 받아들이는가? bytes-level identity까지?

### 2. 왜 자율 불가능한지
Arq.app reader가 plist를 어떤 path로 parse하는지 GUI에서만 확인 가능. arq_restore는 plist도 받지만 (Strategy F §6.1 docs), GUI는 다른 path일 수 있음.

### 3. 운영자 단계
```bash
# 1. 우리 writer를 plist mode로 (현재 CLI에서 노출 안 됨 — 
#    내부 API 호출 필요)
python3 -c "
from arq_writer.backuprecord import serialize_backuprecord
# ... binary plist로 직접 emit
"
# 2. Arq.app GUI로 destination open → record visible?
# 3. Restore 시도 → 성공/실패
```

### 4. 성공 / 실패
- 성공: GUI가 binary-plist record를 정상 표시 + restore 가능
- 실패: GUI가 record를 보지 못하거나 corrupt 에러

### 5. 기존 자율과의 관계
D10 (PR #140)에서 plist↔JSON 라운드트립을 검증. GUI 수용성은 미검증.

---

## P6 — Keyset rotation cross-tool (H1, H2)

### 1. 무엇을 검증하는지
- H1: 우리가 `rotate_keyset_password`로 회전한 keyset을 Arq.app GUI가 받는가?
- H2: Arq.app GUI에서 회전한 keyset을 우리 reader가 받는가?

### 2. 왜 자율 불가능한지
GUI에서 keyset password 변경 → 새 password로 unlock 검증 단계가 필요. AppleScript로는 password 입력 자동화가 SecureInput 때문에 어려움.

### 3. 운영자 단계
```bash
# H1: 우리 → Arq.app
python3 -m arq_validator.crypto rotate \
    --dest /path/to/dest \
    --old-password "old" \
    --new-password "new"
# Arq.app GUI에서 destination open → new password 입력 → 정상?
```

```
# H2: Arq.app → 우리
# Arq.app GUI에서 destination password 변경
# 우리 reader로 read 시도
```

### 4. 성공 / 실패
양방향 호환성 확인 또는 키 derivation 차이 노출.

### 5. 기존 자율과의 관계
Round 1-2에서 keyset writer/reader 자체는 검증; cross-tool은 미검증.

---

## P7 — System clock jump (J3)

### 1. 무엇을 검증하는지
운영자 시스템 clock이 갑자기 future로 점프 (예: NTP 동기 후 5년 미래) → 그 시점에 백업 → record.creationDate가 미래 → 이후 retention 정책이 어떻게 계산?

### 2. 왜 자율 불가능한지
시스템 clock 변경은 system-wide 영향. `date` 명령으로 변경 가능하지만 다른 process(launchd, network sync, cron)에 영향 가능. 운영자가 분리된 환경(VM)에서 진행하는 게 안전.

### 3. 운영자 단계
```bash
# VM에서:
sudo date 052612001980  # 1980년으로 변경
python3 -m arq_writer create ... /tmp/clock-test
# 본 record의 creationDate 확인
# clock 복원 후 retention 정책 적용 → 어떻게?
```

### 4. 성공 / 실패
- 성공: retention이 absolute time이 아닌 relative diff로 계산 → 미래 record가 silently 처리
- 실패: 미래 record가 retention이 영원히 안 잡혀 destination이 비대화

### 5. 기존 자율과의 관계
N10 (locale × TZ)에서 TZ 차이를 다뤘지만 clock skew는 별개.

---

## P8 — Real network latency (J1)

### 1. 무엇을 검증하는지
SFTP backend가 RTT 200ms+ 환경에서 robust한가?

### 2. 왜 자율 불가능한지
`tc qdisc add dev lo root netem delay 200ms`로 시뮬레이션 가능하지만 system-wide 영향. 운영자가 controlled 환경에서 진행 권장.

### 3. 운영자 단계
```bash
# Linux: tc qdisc로 시뮬레이션
# 또는 real remote SFTP server 사용
SFTP_HOST=<real-remote> python3 -m unittest tests.test_sftp_*
```

### 4. 성공 / 실패
Throughput 합리적, partial transfer 시 자동 retry / resume.

### 5. 기존 자율과의 관계
SFTP backend는 paramiko의 retry semantics를 의존; 우리는 별도 retry 안 함.

---

## 운영자가 보고할 내용 (template)

각 항목 진행 후 sandbox에 paste할 정보:

```
Item: P<N>
Date: YYYY-MM-DD
Arq.app version: 7.41 (or other)
Result: SUCCESS | FAILURE | PARTIAL
Observed behavior:
  - <단계별 무엇이 일어났는지>
Arq.app log excerpt (~/Library/Logs/ArqAgent/*.log):
  - <에러 시 마지막 20줄>
Action item for sandbox:
  - <Sandbox가 어떤 후속 작업을 해야 하는지>
```

---

## 우선순위 추천

| 항목 | 우선순위 | 이유 |
|---|---|---|
| **P1** Strategy I | 🔴 최고 | Arq.app GUI 수용성의 정의적 증명; Round 7-10의 모든 작업이 결국 이걸로 closed-loop |
| **P2** K4 first-walk-time | 🟡 중 | trailing-block 의미 확정; 우리 writer 동작에는 영향 없지만 K-series의 마지막 갭 |
| **P6** keyset rotation cross-tool | 🟡 중 | 운영자가 키 변경 시나리오를 실제로 만날 수 있음 |
| **P3** mixed Arq5/6/7 | 🟢 낮 | 운영자 환경에 해당 케이스가 있을 때만 |
| **P4** A1/A2/A3 데몬 동시성 | 🟢 낮 | 운영자가 active 데몬 + 우리 writer를 같은 destination에 쓰지 않는다면 불필요 |
| **P5** binary-plist GUI 수용성 | 🟢 낮 | 현재 우리 default가 JSON이므로 emit 변경 시에만 |
| **P7** clock jump | ⚪ 옵션 | edge case |
| **P8** real network latency | ⚪ 옵션 | SFTP backend 실 사용 시 |

P1이 압도적으로 가치 큼. 주말에 1-2시간이면 끝.
