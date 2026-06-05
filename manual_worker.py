"""수동 다운로드 워커 — 작품 URL 하나에 대해 무료 회차 전체 직렬 다운로드."""
import os
import re
import threading
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List

from .client import (NaverToonClient, NaverToonError,
                     AuthRequiredError, NotReadableError)
from .model import ModelNaverToonItem
from .setup import *  # P, db, logger
from . import worker as _wkr  # ensure_title_metadata 재사용


def _safe_filename(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|]', '_', s or '')
    return s.strip().strip('.')


_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    'status': 'idle',
    'message': '',
    'title_id': None,
    'content_title': '',
    'started_at': None,
    'finished_at': None,
    'episodes': [],
    'current_index': -1,
    'total_to_download': 0,
    'completed': 0,
    'skipped': 0,
    'failed': 0,
    # info.xml / cover.jpg 생성용 — analyze 시 보관 (UI 노출 안 함)
    '_meta': None,
    '_articles': None,
}
_cancel_flag = threading.Event()
_thread: Optional[threading.Thread] = None


def get_state() -> Dict[str, Any]:
    with _state_lock:
        snap = {k: v for k, v in _state.items()
                if k != 'episodes' and not k.startswith('_')}
        snap['episodes'] = [dict(e) for e in _state['episodes']]
        return snap


def _set(**kw):
    with _state_lock:
        _state.update(kw)


def _reset_state():
    with _state_lock:
        _state.update({
            'status': 'idle', 'message': '',
            'title_id': None, 'content_title': '',
            'started_at': None, 'finished_at': None,
            'episodes': [], 'current_index': -1,
            'total_to_download': 0,
            'completed': 0, 'skipped': 0, 'failed': 0,
            '_meta': None, '_articles': None,
        })


def is_running() -> bool:
    with _state_lock:
        return _state['status'] in ('analyzing', 'running')


def cancel():
    _cancel_flag.set()
    _set(message='취소 요청됨')


def analyze(url_or_id: str) -> Dict[str, Any]:
    """URL → 작품 메타 + 회차 목록. 다운로드는 안 함."""
    P.logger.info('[manual] analyze BEGIN url_or_id=%r', url_or_id)
    title_id = NaverToonClient.extract_title_id(url_or_id)
    if not title_id:
        return {'ret': 'fail', 'msg': f'URL에서 titleId 추출 실패: {url_or_id!r}'}

    cookies_json = (P.ModelSetting.get('cookies_json') or '').strip()
    if not cookies_json:
        return {'ret': 'fail', 'msg': '쿠키 미설정 — 설정 페이지에서 쿠키 주입 후 다시 시도'}
    proxy_url = NaverToonClient.resolve_proxy(
        P.ModelSetting.get('use_proxy'), P.ModelSetting.get('proxy_url'))

    try:
        cli = NaverToonClient(cookies_json, logger=P.logger, proxy_url=proxy_url)
    except AuthRequiredError as e:
        return {'ret': 'fail', 'msg': f'쿠키 인증 실패: {e}'}
    except Exception as e:
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'클라이언트 생성 실패: {e}'}

    try:
        meta = cli.get_content(title_id)
    except AuthRequiredError as e:
        return {'ret': 'fail', 'msg': f'권한 만료 — 쿠키 재주입 필요: {e}'}
    except Exception as e:
        return {'ret': 'fail', 'msg': f'content meta 실패: {e}'}

    content_title = (meta.get('titleName') or f'title_{title_id}').strip()

    try:
        articles = cli.get_episodes_all(title_id)
    except Exception as e:
        P.logger.error(traceback.format_exc())
        return {'ret': 'fail', 'msg': f'회차 목록 조회 실패: {e}'}

    if not articles:
        return {'ret': 'fail', 'msg': '회차 없음'}

    # 이미 받은(completed) 회차 — 기본 체크 제외 + 체크 불가 표시용
    completed_nos = set()
    try:
        for r in (db.session.query(ModelNaverToonItem)
                  .filter_by(title_id=title_id).all()):
            if r.status == 'completed' and r.no is not None:
                completed_nos.add(int(r.no))
    except Exception as e:
        P.logger.warning('[manual] completed 조회 실패(계속): %s', e)

    # 전체 회차(무료/유료/잠금)를 표시용으로 모두 반환. selectable=무료&미수신.
    all_eps = []
    for a in articles:
        no = NaverToonClient.episode_no(a)
        avail = NaverToonClient.episode_availability(a)
        completed = no in completed_nos
        all_eps.append({
            'no': no,
            'title': a.get('subtitle', ''),
            'availability': avail,
            'completed': completed,
            'selectable': (avail == 'free') and not completed,
            'state': 'pending',
            'pages_done': 0,
            'pages_total': 0,
            'save_dir': '',
            'error': '',
        })
    all_eps.sort(key=lambda e: e['no'])
    will_download = sum(1 for e in all_eps if e['selectable'])

    # &no= 가 있으면 그 회차만 자동 선택(focus). 무효(없음/유료/완료)면 폴백 안내.
    focus_no = NaverToonClient.extract_episode_no(url_or_id)
    focus_note = ''
    if focus_no is not None:
        match = next((e for e in all_eps if e['no'] == focus_no), None)
        if match is None:
            focus_note = f'지정한 회차({focus_no}) 없음 — 미수신 무료 전체 선택'
            focus_no = None
        elif not match['selectable']:
            focus_note = (f'지정한 회차({focus_no})는 이미 받음 — 미수신 무료 전체 선택'
                          if match['completed']
                          else f'지정한 회차({focus_no})는 유료/잠금 — 받을 수 없음')
            focus_no = None

    _reset_state()
    _set(status='idle',
         message=(f'분석 완료 — 전체 {len(all_eps)}개, '
                  f'받기 가능(무료·미수신) {will_download}개'),
         title_id=title_id, content_title=content_title,
         episodes=all_eps, total_to_download=0,
         _meta=meta, _articles=articles)
    P.logger.info('[manual] analyze END content=%r total=%d selectable=%d focus_no=%s',
                  content_title, len(all_eps), will_download, focus_no)
    return {
        'ret': 'success',
        'title_id': title_id,
        'content_title': content_title,
        'episodes': all_eps,
        'will_download': will_download,
        'total': len(all_eps),
        'focus_no': focus_no,
        'focus_note': focus_note,
    }


def start_selected(selected_nos: List[int]) -> Dict[str, Any]:
    """analyze 로 만든 목록에서 선택된 무료 회차만 다운로드한다.

    selected_nos: 받을 회차 번호 리스트. 무료·미수신(selectable)만 실제 대상.
    """
    global _thread
    P.logger.info('[manual] start_selected BEGIN nos=%s', selected_nos)
    if is_running():
        return {'ret': 'fail', 'msg': '이미 실행 중'}
    with _state_lock:
        if not _state['title_id'] or not _state['episodes']:
            return {'ret': 'fail', 'msg': '먼저 작품을 분석하세요'}
    download_root = (P.ModelSetting.get('download_path') or '').strip()
    if not download_root:
        return {'ret': 'fail', 'msg': 'download_path 미설정'}

    sel = set(int(n) for n in (selected_nos or []))
    with _state_lock:
        targets = [idx for idx, ep in enumerate(_state['episodes'])
                   if ep['no'] in sel and ep.get('selectable')]
    if not targets:
        return {'ret': 'fail',
                'msg': '선택된 무료 회차 없음 (무료·미수신만 선택 가능)'}

    # 전역 락 — 자동/공지/압축/메타 작업과 절대 겹치지 않게 (회차 폴더 zip+삭제
    # 와 다운로드가 겹쳐 폴더가 사라지는 ENOENT 사고 방지). _run 의 finally 에서 해제.
    if not _wkr.try_acquire_run_lock():
        return {'ret': 'fail',
                'msg': '자동 다운로드/공지/압축 등 다른 작업이 실행 중 — 끝난 뒤 다시'}

    target_set = set(targets)
    with _state_lock:
        for idx, ep in enumerate(_state['episodes']):
            if idx in target_set:
                ep['state'] = 'pending'; ep['error'] = ''
                ep['pages_done'] = 0; ep['pages_total'] = 0
            else:
                ep['state'] = 'excluded'

    _cancel_flag.clear()
    _set(status='running', message='선택 다운로드 시작',
         started_at=datetime.now().isoformat(), finished_at=None,
         current_index=-1, completed=0, skipped=0, failed=0,
         total_to_download=len(targets))
    _thread = threading.Thread(target=_run, args=(download_root, targets),
                               daemon=True)
    _thread.start()
    return {'ret': 'success', 'msg': f'{len(targets)}개 회차 다운로드 시작'}


def _run(download_root: str, target_indices: List[int]):
    with F.app.app_context():
        try:
            cookies_json = (P.ModelSetting.get('cookies_json') or '').strip()
            proxy_url = NaverToonClient.resolve_proxy(
                P.ModelSetting.get('use_proxy'),
                P.ModelSetting.get('proxy_url'))
            cli = NaverToonClient(cookies_json, logger=P.logger,
                                  proxy_url=proxy_url)
            with _state_lock:
                title_id = _state['title_id']
                content_title = _state['content_title']
                episodes = list(_state['episodes'])
                meta = _state.get('_meta') or {}
                articles = _state.get('_articles')

            # info.xml / cover.jpg — 첫 다운로드 전에 생성
            try:
                notice_subdir = (P.ModelSetting.get('notice_subdir')
                                 or '완결').strip() or '완결'
                _wkr.ensure_title_metadata(cli, download_root, content_title,
                                           title_id, meta, articles,
                                           group='main',
                                           notice_subdir=notice_subdir)
            except Exception as e:
                P.logger.warning('[manual] ensure_title_metadata 실패: %s', e)

            total = len(target_indices)
            for n, idx in enumerate(target_indices, start=1):
                if _cancel_flag.is_set():
                    _set(status='canceled',
                         finished_at=datetime.now().isoformat(),
                         message='취소됨')
                    return
                ep = episodes[idx]
                _set(current_index=idx)
                P.logger.info('[manual] _run [%d/%d] %s avail=%s no=%s',
                              n, total, ep.get('title'),
                              ep.get('availability'), ep.get('no'))
                ok = _download_episode(cli, title_id, content_title,
                                       idx, ep, download_root)
                with _state_lock:
                    if ok == 'completed':
                        _state['completed'] += 1
                    elif ok == 'skipped':
                        _state['skipped'] += 1
                    else:
                        _state['failed'] += 1

            _set(status='done', finished_at=datetime.now().isoformat(),
                 current_index=-1, message='완료')
        except AuthRequiredError as e:
            _set(status='error', finished_at=datetime.now().isoformat(),
                 message=f'쿠키 만료/무효: {e}')
        except Exception as e:
            P.logger.error('[manual] _run exception: %s', e)
            P.logger.error(traceback.format_exc())
            _set(status='error', finished_at=datetime.now().isoformat(),
                 message=f'에러: {e}')
        finally:
            _wkr.release_run_lock()


def _ep_update(idx: int, **kw):
    with _state_lock:
        _state['episodes'][idx].update(kw)


def _download_episode(cli: NaverToonClient, title_id: int, content_title: str,
                      idx: int, ep: Dict[str, Any], download_root: str) -> str:
    no = ep['no']
    subtitle = ep['title']

    rec = (db.session.query(ModelNaverToonItem)
           .filter_by(title_id=title_id, no=no).first())
    if rec and rec.status == 'completed':
        _ep_update(idx, state='completed', save_dir=rec.save_dir or '',
                   pages_done=rec.downloaded_count or 0,
                   pages_total=rec.page_count or 0)
        return 'completed'
    if rec is None:
        rec = ModelNaverToonItem()
        rec.title_id = title_id
        rec.title_name = content_title
        rec.no = no
        rec.episode_title = subtitle
        db.session.add(rec)
        db.session.commit()

    _ep_update(idx, state='downloading', error='')
    rec.status = 'downloading'; rec.updated_time = datetime.now(); db.session.commit()

    try:
        urls, parsed_subtitle = cli.get_episode_images(title_id, no)
    except NotReadableError as e:
        _ep_update(idx, state='skipped', error=f'잠금: {e}')
        rec.status = 'skipped_locked'; rec.error_msg = str(e); db.session.commit()
        return 'skipped'
    except NaverToonError as e:
        _ep_update(idx, state='failed', error=f'images: {e}')
        rec.status = 'failed'; rec.error_msg = f'images: {e}'; db.session.commit()
        return 'failed'
    if not subtitle and parsed_subtitle:
        subtitle = parsed_subtitle
        rec.episode_title = subtitle

    c_folder = _safe_filename(content_title)
    e_folder = f'{no:04d}_{_safe_filename(subtitle)}'
    save_dir = os.path.join(download_root, c_folder, e_folder)
    os.makedirs(save_dir, exist_ok=True)
    rec.save_dir = save_dir
    _ep_update(idx, save_dir=save_dir, pages_total=len(urls), pages_done=0)
    rec.page_count = len(urls)
    db.session.commit()

    downloaded = 0; total_bytes = 0; failed = 0
    for i, url in enumerate(urls, start=1):
        if _cancel_flag.is_set():
            break
        try:
            data = cli.download_image(url, title_id)
            ext = NaverToonClient.url_ext(url)
            local = os.path.join(save_dir, f'{i:03d}{ext}')
            with open(local, 'wb') as fp:
                fp.write(data)
            total_bytes += len(data)
            downloaded += 1
            _ep_update(idx, pages_done=downloaded)
        except Exception as e:
            failed += 1
            P.logger.warning('manual %s p%s 실패: %s', subtitle, i, e)

    rec.downloaded_count = downloaded
    rec.total_bytes = total_bytes
    rec.downloaded_at = datetime.now()
    rec.updated_time = rec.downloaded_at
    if downloaded == len(urls):
        rec.status = 'completed'
        _ep_update(idx, state='completed')
        db.session.commit()
        # 정상 완료 + 압축 옵션 On → 회차 폴더 ZIP 압축
        if (P.ModelSetting.get('use_compress') or 'False') == 'True':
            from . import worker as _wkr
            zip_path = _wkr.compress_episode_folder(save_dir)
            if zip_path:
                rec.save_dir = zip_path
                db.session.commit()
                _ep_update(idx, save_dir=zip_path)
                P.logger.info('[manual] %s 압축 완료 → %s', subtitle, zip_path)
        return 'completed'
    elif downloaded > 0:
        rec.status = 'partial'
        rec.error_msg = f'failed {failed}/{len(urls)}'
        _ep_update(idx, state='failed', error=f'부분실패 {failed}/{len(urls)}')
        db.session.commit()
        return 'failed'
    else:
        rec.status = 'failed'
        rec.error_msg = f'all failed ({len(urls)})'
        _ep_update(idx, state='failed', error='전부 실패')
        db.session.commit()
        return 'failed'
