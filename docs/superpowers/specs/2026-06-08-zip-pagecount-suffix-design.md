# 압축 파일명 페이지수 접미사(#N) + cbz 인식 + 수동 일괄 부여

작성일: 2026-06-08
대상: naver_toon_dl, kakao_toon_dl, kakaopage_dl (3개 플러그인 공통 패턴)

## 목표

회차 압축 파일명 끝에 페이지수 `#N`을 붙여 뷰어가 페이지 수를 미리 알 수
있게 한다(읽기 속도 향상). 페이지수는 **디스크 파일명에만** 들어가며,
DB·식별·인식 로직은 `#N`을 무시한다. 또한 파일 존재 인식 시 `.zip`뿐
아니라 `.cbz`도 인정한다.

예) `0001_회차제목#25.zip` (이미지 25장). 공백 없음.

## 설계

### A. 새 압축 — 자동 `#N` 부여

- `_zip_episode_folder(ep_folder)` 한 곳에서 파일명 구성:
  `name + '.zip'` → `f'{name}#{len(files_to_zip)}.zip'`.
- 단건(`compress_episode_folder`)·일괄(`compress_all`) 모두 이 함수를 거치므로
  한 곳만 변경하면 전 경로 적용.
- `len(files_to_zip)` = 압축에 담기는 이미지 파일 수. 0개면 압축 대상 아님(현행).
- 새로 만드는 압축은 항상 `.zip`(+`#N`). `.cbz`는 인식 대상일 뿐 생성하지 않음.

### B. 인식 — `.zip` + `.cbz`, `#N` 무시

- 공용 상수/헬퍼:
  - `_ARCHIVE_EXTS = ('.zip', '.cbz')`
  - `_strip_pagecount(s)`: 경로/파일명에서 확장자 앞의 `#\d+` 한 덩어리 제거.
    제목 중간 `#`이나 `#비숫자`는 보존(끝의 `#숫자`만).
- **naver** `_find_episode_zip(series_dir, no)`: `endswith('.zip')` →
  `endswith(_ARCHIVE_EXTS)`. 이미 `{no:04d}_` 접두 매칭이라 `#N`은 자동 무시.
- **kakao_toon / kakaopage**: 인식부가 exact `save_dir + '.zip'` 존재 검사 →
  stem 기반 관용 매칭으로 교체. `dirname(save_dir)`에서
  `basename(save_dir)`(=stem)에 대해 `^{stem}(#\d+)?\.(zip|cbz)$`(대소문자 무시)
  매칭. 자막-정확 동작은 유지하되 `#N`·cbz만 관용.
- 작품폴더 신호/후보 제외 가드(`any(endswith('.zip'))` 지점, `compress_all`
  후보 선정 포함): `.cbz`도 포함하도록 `_ARCHIVE_EXTS`로 확장.

### C. 멱등 — 중복 아카이브 방지

- `_zip_episode_folder`가 새 zip을 만들기 전, 같은 회차 아카이브가 이미
  있으면(stem 일치, `#N`·확장자 무관) 검증 후 재사용:
  - 기존 아카이브 발견 → `_verify_episode_zip(ep_folder, found)` 통과 시 그 경로
    반환(재생성 안 함). 실패 시 손상으로 보고 제거 후 재생성(현행 로직 확장).
- 이로써 "기존 zip 자동 보존"도 자연 충족(기존 `0001_제목.zip` 검증 통과 시 재사용,
  `#N` 버전 새로 안 만듦).

### D. DB는 `#N` 없이 저장

- `rec.save_dir`에 아카이브 경로를 넣는 모든 지점에서 `_strip_pagecount()` 적용.
  디스크 파일명만 `#N`, DB는 `…/0001_제목.zip`.
- 적용 지점(naver): `worker.py` 인식부(1186), 단건 압축 후(1287),
  `manual_worker.py` 압축 후(394). 폴더 경로를 넣는 지점(다운로드 직후)은
  `#N`이 없으므로 변경 불필요. kakao 2개도 동등 지점.
- 안전성: `save_dir`는 쓰기·UI 표시 전용. `os.path.exists(save_dir)`로
  재다운로드를 판단하는 코드는 없음(인식은 별도 디스크 스캔). 따라서 DB의
  깨끗한 경로가 디스크 실제 파일명(`#N`)과 달라도 오작동 없음.

### E. 수동 "페이지수 부여" 버튼

기존 압축 파일에 `#N`을 소급 부여하는 일괄 작업. 기존 `전체압축`/`메타동기화`
버튼과 동일 패턴(백그라운드 스레드 + 진행상황 + 확인창).

- 트리거: command `pagecount_all` → `do_action_pagecount_all` →
  `Worker().add_pagecount_all()` (백그라운드 스레드).
- 동작: `download_path` 아래를 walk → `#N`이 **없는** `.zip`/`.cbz` 회차
  아카이브마다:
  1. 아카이브를 열어 이미지 멤버 수 `N` 집계(`_ARCHIVE_EXTS` 내부의
     `_IMAGE_EXTS` 멤버 수).
  2. `f'{stem}#{N}{ext}'`로 같은 디렉터리에 rename.
- 멱등/안전: 이미 `#N` 있으면 skip; `N==0`/열기 실패/대상 이름 이미 존재 시
  skip + 로그; 원본 삭제 없이 rename만.
- DB는 손대지 않음(이미 `#N` 없는 깨끗한 경로 저장 정책).
- 진행상황 보고: 처리/스킵/실패 카운트.
- 버튼 위치: 설정 페이지 하단 `전체압축`/`메타동기화` 옆. 확인 대화상자.

## 범위 / 버전

- naver_toon_dl → 0.1.20
- kakao_toon_dl → 1.0.35
- kakaopage_dl → 1.0.31
- 세 repo 동일 패턴, 각각 별도 커밋·푸시.

## 테스트

- 오프라인 단위:
  - `_strip_pagecount`: 끝 `#숫자` 제거, 제목 중간 `#`/`#비숫자`/확장자 보존.
  - 아카이브 탐색: `stem(#\d+)?\.(zip|cbz)` 매칭, 무관 파일 제외.
  - 네이밍: 이미지 N장 → `…#N.zip`.
  - 멱등: 같은 폴더 재압축 시 중복 zip 미생성(기존 #N/비#N/cbz 재사용).
  - 수동 부여: `#N` 없는 zip/cbz → `#N` 부여, 이미 있으면 skip, 충돌 skip.
- adversarial 검증 워크플로(인식 누락·중복·DB 오염·rename 충돌·회귀) 통과 후 푸시.

## 엣지 / 비목표

- `#`은 로컬 파일명에 안전(URL 프래그먼트 의미는 로컬 파일과 무관).
- 수동 버튼은 cbz도 rename 대상(뷰어 메타 일관성). zip만 원하면 추후 옵션화.
- 작품 전체압축/메타 생성(info.xml/cover.jpg) 로직은 변경하지 않음.
