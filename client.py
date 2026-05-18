"""네이버웹툰 BFF + 뷰어 HTML 클라이언트.

HAR (검색/목록/뷰어) 분석 기반.

쿠키는 Cookie-Editor 등으로 export 한 `.naver.com` 도메인 쿠키 JSON 그대로 받음.
필수 쿠키: NID_AUT, NID_SES (네이버 통합 로그인). 미로그인 시
        무료/공개 회차 정도만 접근 가능, 로그인 한정 회차는 차단됨.
"""
import io
import json
import re
from datetime import date
from typing import Optional, List, Dict, Any, Tuple

import requests


WEB = 'https://comic.naver.com'

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36')


class NaverToonError(Exception):
    pass


class AuthRequiredError(NaverToonError):
    """쿠키 없음/만료 — 사용자에게 재주입 요청 신호."""


class NotReadableError(NaverToonError):
    """회차가 잠금 상태 (유료/쿠키 결제/연령 제한 등)."""


class NaverToonClient:

    def __init__(self, cookies_json: str, logger=None, proxy_url: str = None):
        self.logger = logger
        self._proxy_url = (proxy_url or '').strip() or None
        self._parse_cookies(cookies_json)

    # ---- 내부 ----
    def _log(self, level: str, msg: str, *args):
        if self.logger:
            getattr(self.logger, level, self.logger.info)(msg, *args)
        else:
            print(f'[{level.upper()}] ' + (msg % args if args else msg))

    def _parse_cookies(self, cookies_json):
        if isinstance(cookies_json, list):
            data = cookies_json
        else:
            s = (cookies_json or '').strip()
            if not s:
                raise AuthRequiredError('cookies_json 비어있음')
            data = json.loads(s)
        self.cookies = []
        for c in data:
            if not c.get('name'):
                continue
            self.cookies.append({
                'name': c['name'], 'value': c.get('value', ''),
                'domain': c.get('domain', '.naver.com'),
                'path': c.get('path', '/'),
            })
        if not any(c['name'] == 'NID_AUT' for c in self.cookies):
            raise AuthRequiredError(
                '필수 쿠키 NID_AUT 없음 — comic.naver.com 로그인 후 재주입 필요')

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            'User-Agent': DEFAULT_UA,
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'ko',
            'Referer': WEB + '/',
        })
        if self._proxy_url:
            s.proxies = {'http': self._proxy_url, 'https': self._proxy_url}
        for c in self.cookies:
            try:
                s.cookies.set(c['name'], c['value'],
                              domain=c['domain'].lstrip('.'),
                              path=c['path'])
            except Exception:
                pass
        return s

    def _json(self, r: requests.Response) -> Any:
        try:
            return r.json()
        except Exception:
            raise NaverToonError(f'invalid JSON ({r.status_code}): {r.text[:200]}')

    def _check_http(self, r: requests.Response):
        if r.status_code in (401, 403):
            raise AuthRequiredError(f'HTTP {r.status_code}: 인증 필요 — 쿠키 재주입')
        if r.status_code >= 400:
            raise NaverToonError(f'HTTP {r.status_code}: {r.text[:200]}')

    # ---- 로그인 확인 ----
    def verify(self) -> bool:
        """쿠키가 로그인 세션으로 유효한지 확인.

        `/api/login/status` 응답의 `naverLogin`/`login` true 면 OK.
        """
        s = self._session()
        try:
            r = s.get(f'{WEB}/api/login/status', timeout=10)
            if r.status_code in (401, 403):
                self._log('info', 'verify → %d (auth fail)', r.status_code)
                return False
            if r.status_code != 200:
                self._log('info', 'verify → %d', r.status_code)
                return False
            body = r.json()
            ok = bool(body.get('naverLogin') or body.get('login'))
            self._log('info', 'verify OK=%s body=%s', ok, body)
            return ok
        except Exception as e:
            self._log('info', 'verify 예외: %s', e)
            return False

    # ---- 공지 ----
    def get_notice_list(self, page: int = 1) -> Dict[str, Any]:
        """공지 목록 (`/api/notice/list`).

        반환: {bestNoticeList[], generalNoticeList[], pageInfo}.
        각 항목: {noticeId, type, subject, bestYn, fileYn, registerDate(ms)}.
        """
        s = self._session()
        s.headers['Referer'] = WEB + '/notice/list'
        r = s.get(f'{WEB}/api/notice/list',
                  params={'page': page} if page > 1 else None, timeout=15)
        self._check_http(r)
        return self._json(r) or {}

    def get_notice_detail(self, notice_id: int) -> Dict[str, Any]:
        """공지 본문 (`/api/notice/detail?noticeId=...`).

        반환: {notice: {..., content(HTML)}, prevNotice, nextNotice, imagePath}.
        """
        s = self._session()
        s.headers['Referer'] = WEB + '/notice/list'
        r = s.get(f'{WEB}/api/notice/detail',
                  params={'noticeId': notice_id, 'searchWord': ''}, timeout=15)
        self._check_http(r)
        return self._json(r) or {}

    @staticmethod
    def parse_paid_notice_subject(subject: str) -> Optional[Tuple[int, int]]:
        """공지 제목이 유료화 전환 공지인지 판별 → (year, month).

        예: '2026년 5월 유료화 전환 작품 안내' → (2026, 5).
            '<완결안내> 작품 ... 안내드립니다.' → None.
        """
        if not subject:
            return None
        m = re.search(r'(\d{4})\s*년\s*(\d{1,2})\s*월\s*유료화', subject)
        if not m:
            return None
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12:
                return (y, mo)
        except Exception:
            pass
        return None

    @staticmethod
    def parse_paid_notice_content(content_html: str,
                                  default_year: Optional[int] = None
                                  ) -> Tuple[Optional[date], List[Dict[str, Any]]]:
        """유료화 공지 본문(HTML) → (전환일, 작품 목록).

        본문 패턴:
          첫 문단: '2026년 5월 12일 (화) 부터 유료로 전환될 예정인 작품을 안내드립니다.'
          이후: <a href=".../webtoon/list?titleId=NNN" ...>작품명</a> 형태로 작품 N개

        반환:
          (date(YYYY,M,D) or None, [{'title_id': int, 'title_name': str}, ...])
        """
        if not content_html:
            return None, []

        # 1) 전환일 추출
        conv_date: Optional[date] = None
        m = re.search(r'(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일',
                      content_html[:1500])
        if m:
            try:
                conv_date = date(int(m.group(1)), int(m.group(2)),
                                 int(m.group(3)))
            except Exception:
                conv_date = None
        else:
            # 4자리 년도 누락된 경우 (예: "5월 12일") — default_year fallback
            m = re.search(r'(\d{1,2})\s*월\s*(\d{1,2})\s*일',
                          content_html[:1500])
            if m and default_year:
                try:
                    conv_date = date(default_year, int(m.group(1)),
                                     int(m.group(2)))
                except Exception:
                    conv_date = None

        # 2) 작품 목록 추출
        import html as _html
        titles: List[Dict[str, Any]] = []
        seen: set = set()
        for am in re.finditer(
                r'<a\b[^>]*href="[^"]*[?&]titleId=(\d+)[^"]*"[^>]*>'
                r'(.*?)</a>', content_html, re.DOTALL | re.IGNORECASE):
            tid = int(am.group(1))
            inner = re.sub(r'<[^>]+>', '', am.group(2))
            name = _html.unescape(inner).strip()
            if not name or tid in seen:
                continue
            seen.add(tid)
            titles.append({'title_id': tid, 'title_name': name})
        return conv_date, titles

    def find_latest_paid_notice(self, max_pages: int = 2
                                ) -> Optional[Dict[str, Any]]:
        """공지 목록에서 가장 최근 '유료화 전환' 공지 1개 반환.

        반환: {noticeId, subject, year, month, registerDate(ms)} or None.
        """
        candidates: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            try:
                body = self.get_notice_list(page=page)
            except NaverToonError as e:
                self._log('warning', '공지 목록 page=%d 실패: %s', page, e)
                break
            for src_key in ('bestNoticeList', 'generalNoticeList'):
                for it in (body.get(src_key) or []):
                    ym = self.parse_paid_notice_subject(it.get('subject') or '')
                    if ym is None:
                        continue
                    candidates.append({
                        'noticeId': int(it.get('noticeId') or 0),
                        'subject': it.get('subject') or '',
                        'year': ym[0], 'month': ym[1],
                        'registerDate': int(it.get('registerDate') or 0),
                    })
            # 1페이지만으로도 보통 충분
            if candidates:
                break
        if not candidates:
            return None
        # 가장 최신: (year, month) DESC → registerDate DESC → noticeId DESC
        candidates.sort(key=lambda c: (c['year'], c['month'],
                                       c['registerDate'], c['noticeId']),
                        reverse=True)
        return candidates[0]

    # ---- 검색 / 메타 ----
    def search_all(self, keyword: str) -> Dict[str, Any]:
        """`/api/search/all` 원본 응답.

        Top keys: searchWebtoonResult, searchBestChallengeResult, searchChallengeResult,
                  searchNbooksComicResult, searchNbooksNovelResult
        각 result 안에 totalCount, searchViewList[].
        """
        s = self._session()
        r = s.get(f'{WEB}/api/search/all',
                  params={'keyword': keyword}, timeout=15)
        self._check_http(r)
        return self._json(r) or {}

    def search_webtoon(self, keyword: str) -> List[Dict]:
        """정식 연재 웹툰만 결과로 반환. (best-challenge / challenge / nbooks 제외)"""
        body = self.search_all(keyword)
        return ((body.get('searchWebtoonResult') or {}).get('searchViewList')
                or [])

    def find_content(self, title: str) -> Optional[Dict]:
        """제목으로 정식 웹툰 검색 → 가장 정확한 매치 하나 반환.

        반환 dict 의 핵심 필드: titleId, titleName, finished, communityArtists 등.
        """
        items = self.search_webtoon(title)
        for it in items:
            if it.get('titleName') == title:
                return it
        return items[0] if items else None

    def get_content(self, title_id: int) -> Dict:
        """작품 메타 (`/api/article/list/info`).

        반환 dict 의 핵심 필드:
          titleName, contentsNo, webtoonLevelCode, rest, finished, dailyPass,
          publishDayOfWeekList, communityArtists, synopsis, age, ...
        """
        s = self._session()
        s.headers['Referer'] = WEB + f'/webtoon/list?titleId={title_id}'
        r = s.get(f'{WEB}/api/article/list/info',
                  params={'titleId': title_id}, timeout=15)
        self._check_http(r)
        return self._json(r) or {}

    def get_episodes_page(self, title_id: int, page: int = 1,
                          sort: str = 'DESC') -> Tuple[List[Dict], Dict]:
        """회차 한 페이지. (articleList, pageInfo) 반환.

        articleList[] 의 핵심 필드: no, subtitle, charge, up, serviceDateDescription,
                                   thumbnailLock, thumbnailClock, volumeNo
        pageInfo: {totalRows, pageSize, page, totalPages, ...}
        """
        s = self._session()
        s.headers['Referer'] = WEB + f'/webtoon/list?titleId={title_id}'
        params = {'titleId': title_id, 'page': page}
        if sort:
            params['sort'] = sort
        r = s.get(f'{WEB}/api/article/list', params=params, timeout=15)
        self._check_http(r)
        body = self._json(r) or {}
        arts = body.get('articleList') or []
        page_info = body.get('pageInfo') or {}
        return arts, page_info

    def get_episodes_all(self, title_id: int, on_progress=None,
                         max_pages: int = 200) -> List[Dict]:
        """모든 회차 (page=1..totalPages) 수집."""
        out: List[Dict] = []
        seen: set = set()
        page = 1
        while page <= max_pages:
            try:
                arts, page_info = self.get_episodes_page(title_id, page=page)
            except NaverToonError as e:
                self._log('warning', '회차 페이지 %d 실패: %s', page, e)
                break
            new = 0
            for a in arts:
                no = a.get('no')
                if no is None or no in seen:
                    continue
                seen.add(no)
                out.append(a)
                new += 1
            if on_progress:
                try:
                    on_progress(len(out))
                except Exception:
                    pass
            total_pages = int(page_info.get('totalPages') or 1)
            if new == 0 or page >= total_pages:
                break
            page += 1
        return out

    # ---- 회차 이미지 ----
    def get_episode_images(self, title_id: int, no: int) -> Tuple[List[str], str]:
        """뷰어 HTML 에서 본문 이미지 URL 추출.

        반환: (image_urls, subtitle).
        잠금 회차/연령제한 등으로 이미지가 없으면 NotReadableError.
        """
        s = self._session()
        s.headers.update({
            'Accept': ('text/html,application/xhtml+xml,application/xml;'
                       'q=0.9,*/*;q=0.8'),
            'Referer': WEB + f'/webtoon/list?titleId={title_id}',
        })
        r = s.get(f'{WEB}/webtoon/detail',
                  params={'titleId': title_id, 'no': no}, timeout=20)
        if r.status_code in (401, 403):
            raise AuthRequiredError(f'detail HTTP {r.status_code} — 쿠키 재주입')
        if r.status_code != 200:
            raise NaverToonError(f'detail HTTP {r.status_code}')
        html = r.text or ''
        # 본문 이미지: <img src="..." alt="comic content" id="content_image_N">
        urls = re.findall(
            r'<img\s+src="(https://image-comic\.pstatic\.net/webtoon/\d+/\d+/[^"]+)"'
            r'\s+alt="comic content"\s+id="content_image_\d+"',
            html)
        if not urls:
            # 부가 fallback: id 패턴 변경/공백 차이 대응
            urls = re.findall(
                r'<img[^>]+id="content_image_\d+"[^>]*src="'
                r'(https://image-comic\.pstatic\.net/webtoon/\d+/\d+/[^"]+)"',
                html)
        if not urls:
            # 로그인 미인증/유료/연령제한 — 페이지엔 다른 안내가 나옴
            if 'login' in r.url.lower() or '로그인' in html[:5000]:
                raise AuthRequiredError('뷰어 진입 시 로그인 페이지로 리다이렉트 — 쿠키 만료')
            raise NotReadableError(
                f'뷰어 본문 이미지 없음 (titleId={title_id}, no={no}) — '
                f'유료/연령제한/잠금 회차일 가능성')
        # 회차 부제(서브타이틀) 추출 — 실패해도 OK
        subtitle = ''
        m = re.search(r'"subtitle":\s*"([^"\\]+)"', html)
        if m:
            subtitle = m.group(1)
        if not subtitle:
            m = re.search(r'<meta property="og:title"[^>]*content="([^"]+)"',
                          html)
            if m:
                subtitle = m.group(1)
        return urls, subtitle

    # ---- 이미지 다운로드 ----
    def download_image(self, url: str, title_id: int) -> bytes:
        """본문 이미지 bytes 다운로드. (referer 강제)"""
        s = self._session()
        s.headers.update({
            'Accept': ('image/avif,image/webp,image/apng,image/svg+xml,'
                       'image/*,*/*;q=0.8'),
            'Referer': WEB + f'/webtoon/detail?titleId={title_id}',
        })
        r = s.get(url, timeout=30)
        if r.status_code != 200:
            raise NaverToonError(f'image fetch {r.status_code} {url[:120]}')
        return r.content

    @staticmethod
    def url_ext(url: str) -> str:
        """이미지 URL 확장자 추출 (`.jpg`/`.png`/`.gif`/...)."""
        m = re.search(r'\.([a-zA-Z0-9]{2,5})(?:\?|$)', url or '')
        if not m:
            return '.jpg'
        ext = '.' + m.group(1).lower()
        if ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'):
            return ext
        return '.jpg'

    # ---- URL 파싱 ----
    @staticmethod
    def extract_title_id(url_or_id: str) -> Optional[int]:
        """네이버웹툰 URL/숫자에서 titleId 추출.

        지원:
          - https://comic.naver.com/webtoon/list?titleId=183559
          - https://comic.naver.com/webtoon/detail?titleId=183559&no=594
          - https://m.comic.naver.com/webtoon/list?titleId=183559
          - 숫자 (titleId)
        """
        s = (url_or_id or '').strip()
        if not s:
            return None
        if s.isdigit():
            return int(s)
        m = re.search(r'[?&]titleId=(\d+)', s)
        if m:
            return int(m.group(1))
        # fallback: 4자리 이상 숫자
        m = re.search(r'(\d{4,})', s)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def extract_episode_no(url: str) -> Optional[int]:
        """뷰어 URL 에서 회차 번호(no) 추출 (선택)."""
        m = re.search(r'[?&]no=(\d+)', url or '')
        return int(m.group(1)) if m else None

    @staticmethod
    def episode_availability(article: Dict) -> str:
        """회차 메타 → 무료/유료/잠금.

        - charge=False → 'free'
        - charge=True or thumbnailLock=True → 'pay'
        - 그 외 → 'locked'
        """
        if article.get('charge'):
            return 'pay'
        if article.get('thumbnailLock'):
            return 'pay'
        # charge=False 면 무료 (UP 이든 일반이든)
        return 'free'

    @staticmethod
    def episode_no(article: Dict) -> int:
        return int(article.get('no') or 0)

    @staticmethod
    def resolve_proxy(use_proxy, proxy_url) -> str:
        """설정값 → 실제 사용할 프록시 URL. use_proxy=True 이고 URL 있을 때만."""
        try:
            enabled = (str(use_proxy or 'False').strip() == 'True')
        except Exception:
            enabled = False
        if not enabled:
            return ''
        return (proxy_url or '').strip()
