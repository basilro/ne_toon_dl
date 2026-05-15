# ne_toon_dl

네이버웹툰 무료 회차 자동 다운로드 SJVA 플러그인.

## 동작

스케줄러가 돌 때마다 설정에 적힌 작품들을 순회:

1. 작품 검색 (`comic.naver.com/api/search/all`) → titleId 매칭
2. 회차 목록 (`/api/article/list?titleId=...&page=N`) page 기반 페이징 수집
3. 회차 분류
   - `charge=false` → 무료 (다운로드)
   - `charge=true` 또는 `thumbnailLock=true` → 유료/잠금 (스킵)
4. 뷰어 페이지 (`/webtoon/detail?titleId=...&no=...`) HTML 에서 `<img id="content_image_N">` 추출
5. 본문 이미지 직접 다운 (원본 jpg) → `{경로}/{작품}/{NNNN_회차}/{001.jpg ...}` 저장
6. DB(`ne_toon_dl_item`) 에 회차별 이력 기록 — 같은 회차 재다운로드 안 함

> 네이버웹툰은 카카오웹툰과 달리 AES 암호화/티켓 시스템이 없어 단순 이미지 다운로드 흐름이다.

## 유료화 공지 자동 다운로드

매월 말 네이버 공지(`/api/notice/list`)에 `"YYYY년 M월 유료화 전환 작품 안내"` 가 올라온다.
설정에서 `공지 기반 자동 다운` On 시:

1. 공지 1페이지에서 **이번 달** 유료화 공지 1건만 검색 (지난달 공지는 무시)
2. 본문 HTML 에서 작품 27개 가량 추출 (`titleId` + 작품명)
3. 전환일 (예: `2026년 5월 12일`) 이 오늘보다 과거면 자동 스킵 — 이미 유료화돼서 무료 다운 불가
4. 각 작품의 무료 회차 → `{다운로드경로}/완결/{작품}/{NNNN_회차}/` 에 저장
5. `last_paid_notice_id` 기록 — 같은 공지 다시 안 돌림

**스케줄 권장**: `0 9 * * *` (매일 09:00). 매일 공지 1건 확인 — 새 공지 없으면 API 1회만 쓰고 종료. 처리한 공지는 `last_paid_notice_id` 로 기록되어 중복 다운로드 안 함.

**서비스 다운 → 재시작 catch-up**: 매일 스케줄이라 다음날 09시에 자동 처리됨. (`plugin_load` 같은 별도 훅 불필요 — SJVA 의 plugin_load 는 DB bind 등록 전에 호출되어 `ModelSetting` 접근이 안 됨.)

## 설정 (`설정` 메뉴)

| 항목 | 설명 |
|---|---|
| 체크할 웹툰 | 한 줄당 하나. 작품 제목 / URL / titleId(숫자) 모두 가능 |
| 네이버 쿠키 JSON | Cookie-Editor 로 `.naver.com` 도메인 쿠키 export 한 JSON |
| 다운로드 경로 | `{경로}/{작품}/{NNNN_회차}/{001.jpg ...}` |
| 공지 기반 자동 다운 | On: 이번 달 유료화 공지 작품 자동 다운로드 |
| 유료화 작품 저장 폴더명 | 완결 (기본). `{경로}/완결/{작품}/...` 로 분리 저장 |

### 쿠키 주입

1. Chrome 에 [Cookie-Editor](https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) 설치
2. `comic.naver.com` 네이버 로그인
3. Cookie-Editor → Export → JSON → 복사 (`.naver.com` 도메인 쿠키 — `NID_AUT`, `NID_SES` 포함되어야 함)
4. 설정의 "네이버 쿠키" 텍스트박스에 붙여넣기 + 저장
5. "쿠키 검증" 으로 유효 확인

## API 메모

| 엔드포인트 | 용도 |
|---|---|
| `GET /api/login/status` | 로그인 여부 확인 (`{naverLogin, login}`) |
| `GET /api/search/all?keyword=...` | 통합 검색 (top key: searchWebtoonResult 등) |
| `GET /api/article/list/info?titleId=...` | 작품 메타 (titleName, contentsNo, dailyPass, ...) |
| `GET /api/article/list?titleId=...&page=N&sort=DESC` | 회차 페이징 (articleList[], pageInfo) |
| `GET /webtoon/detail?titleId=...&no=...` | 뷰어 HTML — `<img id="content_image_N">` 추출 |
| `GET https://image-comic.pstatic.net/webtoon/{titleId}/{no}/...` | 본문 이미지 (Referer 필요) |
| `GET /api/notice/list` | 공지 목록 (`bestNoticeList`, `generalNoticeList`) |
| `GET /api/notice/detail?noticeId=...` | 공지 본문 HTML (`notice.content`) |
