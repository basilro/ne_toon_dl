# 수동 다운로드 — 개별/선택 회차 다운로드 설계

- 작성일: 2026-06-05
- 대상 버전: v0.1.8
- 범위: **수동 다운로드 페이지에만** 영향. 자동(스케줄러)/공지(완결) 경로는 변경 없음.

## 1. 배경 / 문제

현재 수동 다운로드는 작품 URL(또는 titleId)을 받아 **그 작품의 미수신 무료 회차 전체**를
순차 다운로드한다. "이 한 화만" 또는 "이 몇 화만" 받는 방법이 없다.

- 에피소드/뷰어 URL(`.../detail?titleId=X&no=594`)을 넣어도 `extract_title_id`가
  `titleId`만 뽑고 `no`는 무시 → 작품 전체 무료 회차를 받는다.
- 수동 페이지의 회차 목록은 진행 표시용일 뿐, 회차 선택 기능이 없다.

## 2. 목표

두 기능을 **하나의 "회차 선택" 메커니즘**으로 통합 구현한다.

1. **선택 다운로드(체크박스)** — 분석 후 받을 회차만 골라서 받기.
2. **개별 화 다운로드(`&no=`)** — 에피소드 URL을 넣으면 분석 후 그 회차만 자동 선택.
   → "한 화만 받기"는 선택 다운로드의 특수 케이스가 된다(별도 코드 경로 없음).

## 3. 확정된 결정 (brainstorming)

- **흐름**: `분석 → 선택 → 선택 다운로드` 2단계 (기존 일체형 `시작` 버튼 교체).
- **기본 선택 상태**: 분석 직후 **미수신 무료 회차 전체 체크**(이미 받은 회차 제외).
  단, URL에 `&no=N` 이 있으면 **그 회차만 체크**.
- **유료·잠금 회차**: 체크박스 **비활성**(무료만 선택 가능). 목록에는 그대로 표시.

### 추가 결정 (설계 시 확정)

- **이미 받은(completed) 무료 회차**: `완료` 뱃지 + **체크 불가**.
  재다운로드는 "다운로드 이력"에서 해당 회차 삭제 후 다시 분석하면 가능
  (`_download_episode`가 completed 회차를 스킵하므로, 체크 허용해도 무의미).

## 4. 사용자 흐름 (UI)

수동 다운로드 페이지(`ne_toon_dl_basic_manual.html`):

1. URL/titleId 입력 → **`분석`** 클릭.
2. 회차 목록이 체크박스 표로 표시:
   - 컬럼: `[☑] | # | 회차 | 구분 | 진행 | 상태`, 헤더에 `전체 선택` 체크박스.
   - 무료&미수신 → 체크 가능, 기본 체크. 무료&완료 → 체크 불가. 유료/잠금 → 체크 불가.
   - `&no=N` 지정 시: N이 선택 가능하면 N만 체크, 나머지 해제.
     N이 유료/잠금/목록에 없으면 안내 메시지 + 기본(미수신 무료 전체 체크).
3. 받을 회차만 체크 → **`선택 다운로드`** 클릭.
   - 체크된 무료 회차가 0개면 경고하고 중단.
   - 다운로드 시작 → 1.5초 폴링으로 표가 진행률/상태로 갱신.
   - 미선택 회차 행은 `제외` 상태로 표시.
4. **`취소`** — 진행 중 다운로드 취소(기존 동작 유지).

## 5. 백엔드 설계

### 5.1 client.py
- `NaverToonClient.extract_episode_no(url_or_id) -> Optional[int]` 추가.
  - 정규식 `[?&]no=(\d+)` 매칭 시 정수 반환, 없으면 None.
  - 기존 `extract_title_id` 는 변경하지 않는다.

### 5.2 manual_worker.py
`_state['episodes']` 각 항목 필드(확장):
`no, title, availability, completed(bool), selectable(bool), state, pages_done, pages_total, save_dir, error`
- `selectable = (availability == 'free') and (not completed)`
- `state` 값에 `excluded`(제외) 추가.

- **`analyze(url_or_id)`** 변경:
  - `title_id`, `focus_no = extract_episode_no(url_or_id)` 추출.
  - 회차 **전체**(무료/유료/잠금)를 `no` 오름차순으로 `_state['episodes']` 에 저장.
    각 회차의 `completed` 는 DB(`ModelNaverToonItem` title_id+no)로 판정.
  - 반환: `{ret, title_id, content_title, episodes, total, will_download(=selectable 수),
    focus_no, focus_note}`. (focus_note: focus_no가 무효일 때 사유 메시지)
  - 다운로드는 하지 않는다(기존과 동일).

- **`start_selected(selected_nos: List[int]) -> dict`** (기존 `run_with_url`/`start` 교체):
  - 가드: `is_running()` / 분석된 `title_id`·`episodes` 존재 / `download_root` 설정.
  - **전역 락 획득**: `_wkr.try_acquire_run_lock()` 실패 시 "다른 작업 실행 중" 반환.
  - `targets = [idx for idx,ep in enumerate(episodes) if ep['no'] in selected and ep['selectable']]`
  - `targets` 비면 → 락 해제 후 `{'ret':'fail','msg':'선택된 무료 회차 없음'}`.
  - 비대상 회차 `state='excluded'`, 대상 회차 `state='pending'`,
    `total_to_download=len(targets)`, 카운터 초기화.
  - `_thread = Thread(target=_run, args=(download_root, targets))` 시작 → `{'ret':'success'}`.

- **`_run(download_root, target_indices)`**: `target_indices` 만 순회하며
  `_download_episode(...)` 호출. `finally`에서 `_wkr.release_run_lock()`(이미 적용됨).

> `run_with_url`, `start` 는 제거(어디서도 호출되지 않게 됨).

### 5.3 mod_basic.py — process_command
- `mrun` 분기 제거. 다음 추가:
  - `manalyze` → `manual_worker.analyze(url)` (url = arg1 또는 form). 동기 반환.
  - `mdownload` → arg1(CSV no) 파싱 → `int` 리스트 → `manual_worker.start_selected(list)`.
- `mcancel`, `mprogress` 유지.

### 5.4 템플릿 — ne_toon_dl_basic_manual.html
- 버튼: `manualAnalyzeBtn('분석')`, `manualDownloadBtn('선택 다운로드')`, `manualCancelBtn('취소')`.
- 회차 표에 체크박스 컬럼 + 헤더 `전체 선택` 체크박스 추가(`list.html` 패턴 참고).
- `renderEpisodes`:
  - 체크박스 `disabled = !selectable`. 기본 `checked` 는 **프론트에서 계산**:
    `focus_no` 가 있으면 `no === focus_no` 인 행만, 없으면 `selectable` 인 행 전체.
    (백엔드는 `selectable`/`focus_no` 만 주고, 기본 체크 판단은 프론트가 한다.)
  - `stateBadge` 에 `excluded → 제외` 추가.
- 핸들러:
  - `분석` → `{command:'manalyze', arg1:url}` → 응답으로 표 렌더(체크 상태 계산: focus_no 우선, 없으면 미수신 무료 전체).
  - `선택 다운로드` → 체크된 `no` 수집 → `{command:'mdownload', arg1: nos.join(',')}` → 폴링 시작.
  - `전체 선택` → selectable 행 전체 토글.
  - `취소`/`mprogress` 폴링 → 기존 유지.

## 6. 동시성 / 안전

- `analyze` 는 파일을 쓰지 않으므로 전역 락 불필요(읽기 전용).
- 실제 다운로드(`start_selected` → `_run`)만 전역 락 보유 → 자동/공지/압축/메타와 상호배제
  (v0.1.7 메커니즘 재사용). 회차 폴더 ENOENT 사고 방지 유지.
- 저장 경로는 기존 수동 경로(`download_root/작품/회차`, 완결 하위폴더 없음) 그대로.

## 7. 엣지 케이스

| 상황 | 처리 |
|------|------|
| `&no=N` 이 유료/잠금/목록없음 | focus_note 안내, 기본(미수신 무료 전체 체크)로 폴백 |
| 무료 회차 0개 | 표는 표시(전부 비활성), `선택 다운로드` 시 "선택된 무료 회차 없음" |
| completed 회차 체크 시도 | 체크 불가(disabled) — 재다운로드는 이력 삭제 후 |
| 다운로드 중 다른 작업 실행 중 | 전역 락 실패 → "다른 작업 실행 중" |
| 취소 | `_cancel_flag` → `_run` 중단, `finally`에서 락 해제 |

## 8. 테스트

- `extract_episode_no` 정규식 단독 테스트(개발 PC에서 실행 가능):
  list/detail/m. URL, no 없는 URL, 숫자만 등.
- 선택 필터 로직(`targets` 산출) 순수 함수 점검.
- manual_worker 전체는 flaskfarm 의존 → 실제 동작은 NAS 배포 후 확인.
- 두 파일 `py_compile` 클린 확인.

## 9. 영향 / 비범위

- 영향: 수동 다운로드 페이지 UX 및 관련 커맨드.
- 비범위: 자동 스케줄러, 공지/완결 다운로드, 압축, 메타 동기화 로직.
