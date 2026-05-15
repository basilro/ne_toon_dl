"""스케줄 1회 실행 단위 — 작품 리스트를 돌면서 무료 회차 다운로드."""
import os
import re
import threading
from datetime import datetime, date
from typing import List, Optional, Dict, Any, Tuple

from .client import (NaverToonClient, NaverToonError,
                     AuthRequiredError, NotReadableError)
from .model import ModelNaverToonItem
from .notify import (send_webhook, build_download_summary,
                     build_cookie_expired_message)
from .setup import *  # P, db, logger


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


# ---- 자동 다운로드 진행 상태 (싱글톤) ----
_auto_state_lock = threading.Lock()
_auto_state: Dict[str, Any] = {
    'status': 'idle',
    'started_at': None,
    'finished_at': None,
    'message': '',
    'titles_total': 0,
    'titles_done': 0,
    'current_title': '',
    'current_phase': '',
    'current_episode': '',
    'current_pages_done': 0,
    'current_pages_total': 0,
    'summary': {'downloaded': 0, 'skipped': 0, 'failed': 0},
}


def get_auto_state() -> Dict[str, Any]:
    with _auto_state_lock:
        snap = dict(_auto_state)
        snap['summary'] = dict(_auto_state['summary'])
        return snap


def _auto_set(**kw):
    with _auto_state_lock:
        _auto_state.update(kw)


def _auto_reset():
    with _auto_state_lock:
        _auto_state.update({
            'status': 'idle', 'started_at': None, 'finished_at': None,
            'message': '', 'titles_total': 0, 'titles_done': 0,
            'current_title': '', 'current_phase': '',
            'current_episode': '', 'current_pages_done': 0, 'current_pages_total': 0,
            'summary': {'downloaded': 0, 'skipped': 0, 'failed': 0},
        })


def _auto_summary_inc(key: str, delta: int = 1):
    with _auto_state_lock:
        _auto_state['summary'][key] = _auto_state['summary'].get(key, 0) + delta


class Worker:

    def __init__(self):
        self.cfg = P.ModelSetting.to_dict()
        self.download_root = (self.cfg.get('download_path') or '').strip()
        self.cookies_json = (self.cfg.get('cookies_json') or '').strip()
        self.items: List[str] = self._split_items(self.cfg.get('titles') or '')
        self.notify_cookie_url = (self.cfg.get('notify_webhook_cookie') or '').strip()
        self.notify_download_url = (self.cfg.get('notify_webhook_download') or '').strip()
        self.notice_auto_dl = (self.cfg.get('notice_auto_dl') or 'False') == 'True'
        self.notice_subdir = (self.cfg.get('notice_subdir') or '완결').strip() or '완결'
        self.client: Optional[NaverToonClient] = None
        self.completed_items: List[Dict[str, Any]] = []  # 알림용 누적

    @staticmethod
    def _split_items(raw: str) -> List[str]:
        out = []
        for chunk in (raw or '').replace('\r', '').replace('|', '\n').split('\n'):
            s = chunk.strip()
            if s:
                out.append(s)
        return out

    # ---- public ----
    def run(self) -> dict:
        P.logger.info('[basic] Worker.run BEGIN items=%s', self.items)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='시작', titles_total=len(self.items))
        if not self.download_root:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.cookies_json:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='cookies_json 미설정')
            return {'ret': 'fail', 'reason': 'no_cookies'}
        if not self.items:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='체크할 작품 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = NaverToonClient(self.cookies_json, logger=P.logger)
        except AuthRequiredError as e:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'쿠키 인증 실패: {e}')
            return {'ret': 'fail', 'reason': 'auth', 'msg': str(e)}

        if not self.client.verify():
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='쿠키 만료 — 재주입 필요')
            # 만료 알림 — 1회만 발송 (스팸 방지)
            try:
                already = (P.ModelSetting.get('cookie_expired_notified') or 'False') == 'True'
                if not already and self.notify_cookie_url:
                    if send_webhook(self.notify_cookie_url,
                                    build_cookie_expired_message()):
                        P.ModelSetting.set('cookie_expired_notified', 'True')
            except Exception as e:
                P.logger.warning('쿠키 만료 알림 발송 실패: %s', e)
            return {'ret': 'fail', 'reason': 'cookie_expired'}

        # 정상 verify → 만료 플래그 리셋 (다음 만료 때 다시 1회 알림 가능)
        try:
            if (P.ModelSetting.get('cookie_expired_notified') or 'False') == 'True':
                P.ModelSetting.set('cookie_expired_notified', 'False')
        except Exception:
            pass

        summary = {'titles': len(self.items), 'downloaded': 0, 'skipped': 0, 'failed': 0}
        for raw in self.items:
            _auto_set(current_title=raw, current_phase='searching',
                      current_episode='', current_pages_done=0, current_pages_total=0)
            try:
                got = self._process_item(raw)
                if got == 'downloaded':
                    summary['downloaded'] += 1; _auto_summary_inc('downloaded')
                elif got == 'skipped':
                    summary['skipped'] += 1; _auto_summary_inc('skipped')
                else:
                    summary['failed'] += 1; _auto_summary_inc('failed')
            except Exception as e:
                import traceback
                P.logger.error('process item %r exception: %s', raw, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1; _auto_summary_inc('failed')
            _auto_set(titles_done=summary['downloaded'] + summary['skipped'] + summary['failed'])

        # ---- 유료화 공지 자동 다운 (월말 공지 → 1일 다음 실행 시 catch-up) ----
        if self.notice_auto_dl:
            try:
                ncount = self._process_paid_notices(summary)
                P.logger.info('[basic] notice 처리: %d 작품 추가 다운', ncount)
            except Exception as e:
                import traceback
                P.logger.error('notice 처리 예외: %s', e)
                P.logger.error(traceback.format_exc())

        # ---- 다운로드 완료 요약 알림 (받은 게 있을 때만) ----
        if self.completed_items and self.notify_download_url:
            try:
                msg = build_download_summary(self.completed_items)
                if msg:
                    ok = send_webhook(self.notify_download_url, msg)
                    P.logger.info('다운로드 요약 알림 발송: %s (%d건)',
                                  'OK' if ok else 'FAIL', len(self.completed_items))
            except Exception as e:
                P.logger.warning('다운로드 요약 알림 예외: %s', e)

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"완료 — 다운 {summary['downloaded']}, 스킵 {summary['skipped']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ---- per item ----
    def _process_item(self, raw: str) -> str:
        tid = NaverToonClient.extract_title_id(raw)
        if tid:
            title_id = tid
            display = raw
            P.logger.info('[%s] title_id 직접: %s', raw, title_id)
        else:
            it = self.client.find_content(raw)
            if not it:
                P.logger.warning('[%s] 검색 실패', raw)
                return 'failed'
            title_id = it['titleId']
            display = it.get('titleName') or raw
            P.logger.info('[%s] 검색→ title_id=%s title=%r', raw, title_id, display)

        return self._process_title(display, title_id)

    def _process_title(self, title: str, title_id: int) -> str:
        _auto_set(current_phase='fetch_episodes')
        try:
            meta = self.client.get_content(title_id)
            if meta.get('titleName'):
                title = meta['titleName']
                _auto_set(current_title=title)
        except NaverToonError as e:
            P.logger.warning('[%s] content meta 실패 (계속): %s', title, e)

        try:
            articles = self.client.get_episodes_all(title_id)
        except NaverToonError as e:
            P.logger.error('[%s] 회차 조회 실패: %s', title, e)
            return 'failed'
        if not articles:
            P.logger.warning('[%s] 회차 없음', title)
            return 'failed'

        # 분류 — 무료만 받음
        free: List[Dict] = []
        for a in articles:
            no = a.get('no')
            if no is None:
                continue
            rec = (db.session.query(ModelNaverToonItem)
                   .filter_by(title_id=title_id, no=no).first())
            if rec and rec.status == 'completed':
                continue
            avail = NaverToonClient.episode_availability(a)
            if avail == 'free':
                free.append(a)
        free.sort(key=NaverToonClient.episode_no)
        paid_cnt = sum(1 for a in articles
                       if NaverToonClient.episode_availability(a) != 'free')
        P.logger.info('[%s] 미수신 무료 %d개, 유료/잠금 %d개',
                      title, len(free), paid_cnt)

        if not free:
            return 'skipped'

        downloaded_count = 0
        _auto_set(current_phase='downloading')
        for a in free:
            _auto_set(current_episode=a.get('subtitle', ''),
                      current_pages_done=0, current_pages_total=0)
            if self._download_one(title, title_id, a) == 'downloaded':
                downloaded_count += 1

        return 'downloaded' if downloaded_count else 'skipped'

    # ---- 유료화 공지 처리 ----
    def _process_paid_notices(self, summary: Dict[str, int]) -> int:
        """가장 최근 '유료화 전환' 공지에서 작품을 추출 → 무료 회차 다운.

        전환일이 이미 과거(<오늘)면 자동 스킵 (유료화돼서 무료 다운 불가).
        last_paid_notice_id 설정값으로 중복 처리 방지 — 같은 공지는 한 번만.
        """
        _auto_set(current_phase='fetch_notice', current_title='[유료화 공지]',
                  current_episode='', current_pages_done=0, current_pages_total=0)
        latest = self.client.find_latest_paid_notice()
        if latest is None:
            P.logger.info('[notice] 유료화 전환 공지 없음')
            return 0

        today = date.today()
        # 1) "이번 달" 공지만 처리 — 4월/지난달 공지는 보지 않음.
        if latest['year'] != today.year or latest['month'] != today.month:
            P.logger.info('[notice] 최신 공지가 이번 달이 아님 — skip '
                          '(공지 %d년 %d월, 오늘 %d년 %d월)',
                          latest['year'], latest['month'],
                          today.year, today.month)
            return 0

        try:
            last_id = int((P.ModelSetting.get('last_paid_notice_id') or '0') or 0)
        except Exception:
            last_id = 0
        if latest['noticeId'] <= last_id:
            P.logger.info('[notice] 최신 유료화 공지 noticeId=%s 이미 처리됨 (last=%s)',
                          latest['noticeId'], last_id)
            return 0

        P.logger.info('[notice] 대상 공지 noticeId=%s subject=%r',
                      latest['noticeId'], latest['subject'])
        try:
            body = self.client.get_notice_detail(latest['noticeId'])
        except NaverToonError as e:
            P.logger.warning('[notice] detail 실패: %s', e)
            return 0
        content_html = ((body.get('notice') or {}).get('content')) or ''

        conv_date, items = NaverToonClient.parse_paid_notice_content(
            content_html, default_year=latest['year'])
        P.logger.info('[notice] 전환일=%s, 작품 %d개', conv_date, len(items))

        if conv_date is not None and conv_date < today:
            P.logger.info('[notice] 전환일 %s 가 이미 지남(오늘 %s) — 스킵',
                          conv_date, today)
            try:
                P.ModelSetting.set('last_paid_notice_id', str(latest['noticeId']))
            except Exception:
                pass
            return 0

        if not items:
            P.logger.info('[notice] 작품 추출 실패 — 본문 미리보기: %s',
                          re.sub(r'<[^>]+>', ' ', content_html[:400]))
            return 0

        processed = 0
        for it in items:
            title_id = it['title_id']
            title_name = it['title_name']
            _auto_set(current_title=f'[유료화] {title_name}',
                      current_phase='fetch_episodes',
                      current_episode='', current_pages_done=0, current_pages_total=0)
            try:
                got = self._process_paid_title(title_id, title_name)
                if got == 'downloaded':
                    summary['downloaded'] += 1; _auto_summary_inc('downloaded')
                    processed += 1
                elif got == 'skipped':
                    summary['skipped'] += 1; _auto_summary_inc('skipped')
                else:
                    summary['failed'] += 1; _auto_summary_inc('failed')
            except Exception as e:
                P.logger.warning('[notice] %s 처리 실패: %s', title_name, e)
                summary['failed'] += 1; _auto_summary_inc('failed')

        # 정상 처리 끝 → 같은 공지 다시 안 돌게 기록
        try:
            P.ModelSetting.set('last_paid_notice_id', str(latest['noticeId']))
            P.logger.info('[notice] last_paid_notice_id=%s 저장',
                          latest['noticeId'])
        except Exception as e:
            P.logger.warning('[notice] last_paid_notice_id 저장 실패: %s', e)
        return processed

    def _process_paid_title(self, title_id: int, title_name: str) -> str:
        """공지로 받은 작품 1개 — 메타 보강 후 무료 회차 모두 완결 폴더에 다운."""
        try:
            meta = self.client.get_content(title_id)
            if meta.get('titleName'):
                title_name = meta['titleName']
                _auto_set(current_title=f'[유료화] {title_name}')
        except NaverToonError as e:
            P.logger.warning('[notice] %s 메타 실패(계속): %s', title_name, e)

        try:
            articles = self.client.get_episodes_all(title_id)
        except NaverToonError as e:
            P.logger.warning('[notice] %s 회차 조회 실패: %s', title_name, e)
            return 'failed'
        if not articles:
            return 'failed'

        free: List[Dict] = []
        for a in articles:
            no = a.get('no')
            if no is None:
                continue
            rec = (db.session.query(ModelNaverToonItem)
                   .filter_by(title_id=title_id, no=no).first())
            if rec and rec.status == 'completed':
                continue
            if NaverToonClient.episode_availability(a) == 'free':
                free.append(a)
        free.sort(key=NaverToonClient.episode_no)
        if not free:
            P.logger.info('[notice] %s 새로 받을 무료 회차 없음', title_name)
            return 'skipped'

        downloaded = 0
        _auto_set(current_phase='downloading')
        for a in free:
            _auto_set(current_episode=a.get('subtitle', ''),
                      current_pages_done=0, current_pages_total=0)
            if self._download_one(title_name, title_id, a,
                                  group='complete') == 'downloaded':
                downloaded += 1
        return 'downloaded' if downloaded else 'skipped'

    # ---- one episode ----
    def _download_one(self, title_name: str, title_id: int,
                      article: Dict[str, Any],
                      group: str = 'main') -> str:
        no = NaverToonClient.episode_no(article)
        subtitle = article.get('subtitle') or ''

        rec = (db.session.query(ModelNaverToonItem)
               .filter_by(title_id=title_id, no=no).first())
        if rec and rec.status == 'completed':
            return 'skipped'
        if rec is None:
            rec = ModelNaverToonItem()
            rec.title_id = title_id
            rec.title_name = title_name
            rec.no = no
            rec.episode_title = subtitle
            db.session.add(rec)
            db.session.commit()
        rec.updated_time = datetime.now()

        # ---- 뷰어에서 이미지 URL 추출 ----
        try:
            urls, parsed_subtitle = self.client.get_episode_images(title_id, no)
        except NotReadableError as e:
            rec.status = 'skipped_locked'; rec.error_msg = str(e)
            db.session.commit(); return 'skipped'
        except NaverToonError as e:
            rec.status = 'failed'; rec.error_msg = f'images: {e}'
            db.session.commit(); return 'failed'
        if not subtitle and parsed_subtitle:
            # subtitle 이 article list 에 없으면 뷰어에서 보강
            rec.episode_title = parsed_subtitle
            subtitle = parsed_subtitle

        # ---- 저장 경로 ----
        c_folder = _safe_filename(title_name)
        e_folder = f'{no:04d}_{_safe_filename(subtitle)}'
        if group == 'complete':
            save_dir = os.path.join(self.download_root,
                                    _safe_filename(self.notice_subdir),
                                    c_folder, e_folder)
        else:
            save_dir = os.path.join(self.download_root, c_folder, e_folder)
        os.makedirs(save_dir, exist_ok=True)
        rec.save_dir = save_dir
        rec.status = 'downloading'
        rec.page_count = len(urls)
        db.session.commit()
        _auto_set(current_pages_total=len(urls), current_pages_done=0)

        downloaded = 0; total_bytes = 0; failed: List[Tuple[int, str]] = []
        for i, url in enumerate(urls, start=1):
            try:
                data = self.client.download_image(url, title_id)
                ext = NaverToonClient.url_ext(url)
                local = os.path.join(save_dir, f'{i:03d}{ext}')
                with open(local, 'wb') as fp:
                    fp.write(data)
                total_bytes += len(data)
                downloaded += 1
                _auto_set(current_pages_done=downloaded)
            except Exception as e:
                failed.append((i, str(e)))
                P.logger.warning('[%s] %s page %d 실패: %s',
                                 title_name, subtitle, i, e)

        rec.downloaded_count = downloaded
        rec.total_bytes = total_bytes
        rec.downloaded_at = datetime.now()
        rec.updated_time = rec.downloaded_at
        if downloaded == len(urls):
            rec.status = 'completed'
            P.logger.info('[%s] %s 다운로드 완료 (%d개, %.1fKB)',
                          title_name, subtitle, downloaded, total_bytes / 1024)
            self.completed_items.append({
                'group': group,
                'title_name': title_name,
                'episode_title': subtitle,
                'no': no,
            })
        elif downloaded > 0:
            rec.status = 'partial'
            rec.error_msg = f'failed {len(failed)}/{len(urls)}'
        else:
            rec.status = 'failed'
            rec.error_msg = f'all failed ({len(urls)})'
        db.session.commit()
        return 'downloaded' if rec.status in ('completed', 'partial') else 'failed'
