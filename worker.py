"""스케줄 1회 실행 단위 — 작품 리스트를 돌면서 무료 회차 다운로드."""
import functools
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


_IMAGE_EXTS = ('.webp', '.jpg', '.jpeg', '.png', '.gif', '.bmp')

# 회차 아카이브로 인정하는 확장자 — 생성은 .zip 만, 인식은 .cbz 도 인정.
_ARCHIVE_EXTS = ('.zip', '.cbz')


def _strip_pagecount(name: str) -> str:
    """파일명/경로 끝의 '#<숫자>' 페이지수 표기를 (확장자 앞에서) 제거.

    '0001_제목#25.zip' → '0001_제목.zip'. 제목 중간의 '#'이나 '#비숫자'는 보존
    (끝의 '#숫자'만 제거). DB save_dir 에는 이 정규화된(=#N 없는) 경로를 저장한다.
    """
    root, ext = os.path.splitext(name or '')
    return re.sub(r'#\d+$', '', root) + ext


def _find_episode_zip(series_dir: str, no: int) -> Optional[str]:
    """series_dir 안에서 '{no:04d}_*' 회차 아카이브(.zip/.cbz)를 찾아 경로 반환.

    네이버는 (title_id, no) 로 회차를 식별하고 폴더명의 subtitle 은 뷰어에서
    보강되어 달라질 수 있으므로, subtitle 이 아니라 회차번호(no) 접두로 매칭한다.
    파일명 끝의 '#페이지수' 와 확장자(zip/cbz)는 무시한다.
    """
    if not os.path.isdir(series_dir):
        return None
    prefix = f'{no:04d}_'
    try:
        for fn in sorted(os.listdir(series_dir)):
            if fn.startswith(prefix) and fn.lower().endswith(_ARCHIVE_EXTS):
                return os.path.join(series_dir, fn)
    except OSError:
        return None
    return None


def _find_archive_by_stem(directory: str, stem: str) -> Optional[str]:
    """directory 안에서 stem 과 일치하는 회차 아카이브를 찾는다(없으면 None).

    '{stem}.zip' / '{stem}#<숫자>.zip' / 동일 형태의 .cbz 를 인정 — 페이지수 표기와
    확장자(zip/cbz)는 무시하고 stem 으로 매칭. 멱등 압축에서 같은 회차 아카이브
    중복 생성을 막기 위해 사용.
    """
    if not os.path.isdir(directory):
        return None
    try:
        for fn in sorted(os.listdir(directory)):
            if not fn.lower().endswith(_ARCHIVE_EXTS):
                continue
            base = re.sub(r'#\d+$', '', os.path.splitext(fn)[0])
            if base == stem:
                return os.path.join(directory, fn)
    except OSError:
        return None
    return None


def _count_archive_images(path: str) -> int:
    """아카이브(.zip/.cbz) 내부 이미지 멤버 수를 센다 (열기 실패 시 -1)."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as zf:
            return sum(1 for n in zf.namelist()
                       if not n.endswith('/') and n.lower().endswith(_IMAGE_EXTS))
    except Exception:
        return -1


def _verify_episode_zip(ep_folder: str, zip_path: str) -> bool:
    """zip 이 원본 회차 폴더의 모든 이미지를 동일 이름·크기로 담고 CRC 무결한지 검증.

    삭제 전에 호출해 '압축이 문제없을 때만' 원본을 지우도록 하는 안전장치.
    """
    import zipfile
    if not os.path.isfile(zip_path) or not os.path.isdir(ep_folder):
        return False
    try:
        src = {}
        for f in os.listdir(ep_folder):
            p = os.path.join(ep_folder, f)
            if os.path.isfile(p) and f.lower().endswith(_IMAGE_EXTS):
                src[f] = os.path.getsize(p)
    except Exception:
        return False
    if not src:
        return False
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if zf.testzip() is not None:   # 멤버 CRC 손상
                return False
            infos = {i.filename: i.file_size for i in zf.infolist()}
    except Exception:
        return False
    # 원본 이미지가 모두 같은 이름·크기로 zip 안에 있어야 통과.
    for fn, size in src.items():
        if infos.get(fn) != size:
            return False
    return True


def _zip_episode_folder(ep_folder: str) -> Optional[str]:
    """회차 폴더 → 같은 위치에 .zip 생성. 원본은 삭제하지 않는다 (삭제는 검증 후).

    이미지 파일만 포함. 기존 zip 이 원본과 일치하면 그대로 두고, 손상/불일치면
    재생성. 멱등.

    반환: zip 경로 또는 None (대상 아님/실패).

    안전장치: 폴더 안에 서브디렉토리가 있거나 작품 폴더 신호(info.xml/cover.jpg/
    .zip)가 있으면 회차 폴더가 아니므로 압축 거부.
    """
    import zipfile
    if not os.path.isdir(ep_folder):
        return None

    try:
        entries = os.listdir(ep_folder)
    except Exception:
        return None
    for entry in entries:
        if os.path.isdir(os.path.join(ep_folder, entry)):
            P.logger.warning(
                '압축 거부 (서브디렉토리 존재 → 회차 폴더 아님): %s', ep_folder)
            return None
    # 작품 폴더 신호(info.xml/cover.jpg/이미 압축된 .zip 회차)가 있으면 회차 폴더가
    # 아니므로 거부 — cover.jpg(.jpg)를 이미지로 오인해 작품 폴더를 통째로 zip+rmtree
    # 하던 사고 방지.
    lower = {e.lower() for e in entries}
    if ('info.xml' in lower or 'cover.jpg' in lower
            or any(e.endswith(_ARCHIVE_EXTS) for e in lower)):
        P.logger.warning(
            '압축 거부 (작품 폴더 신호 info.xml/cover.jpg/아카이브 존재 → 회차 폴더 아님): %s',
            ep_folder)
        return None

    files_to_zip = []
    for f in sorted(entries):
        path = os.path.join(ep_folder, f)
        if os.path.isfile(path) and f.lower().endswith(_IMAGE_EXTS):
            files_to_zip.append((f, path))
    if not files_to_zip:
        return None

    parent = os.path.dirname(ep_folder)
    name = os.path.basename(ep_folder)
    # 파일명 끝에 페이지수 #N (이미지 수) — 뷰어 표시용. DB 에는 #N 빼고 저장.
    zip_path = os.path.join(parent, f'{name}#{len(files_to_zip)}.zip')

    # 멱등: 같은 회차 아카이브(stem 일치, #N·확장자 무관)가 이미 있으면 검증 후 재사용.
    existing = _find_archive_by_stem(parent, name)
    if existing:
        if _verify_episode_zip(ep_folder, existing):
            return existing
        try:
            os.remove(existing)
        except Exception as e:
            P.logger.warning('기존 손상 아카이브 삭제 실패 — 건너뜀 %s: %s', existing, e)
            return None

    tmp_zip = zip_path + '.tmp'
    try:
        with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_STORED) as zf:
            for arcname, path in files_to_zip:
                zf.write(path, arcname=arcname)
        os.replace(tmp_zip, zip_path)
    except Exception as e:
        if os.path.exists(tmp_zip):
            try:
                os.remove(tmp_zip)
            except Exception:
                pass
        P.logger.warning('압축 실패 %s: %s', ep_folder, e)
        return None
    return zip_path


def compress_episode_folder(ep_folder: str) -> Optional[str]:
    """회차 폴더 → zip 생성 → 검증 통과 시에만 원본 삭제. 멱등.

    단건/인라인 다운로드용 편의 함수. 일괄 압축(compress_all)은 생성·검증·삭제를
    단계별로 분리해 직접 수행한다.

    반환: 검증까지 통과한 zip 경로, 또는 None (대상 아님/실패/검증 실패 — 원본 보존).
    """
    import shutil
    zip_path = _zip_episode_folder(ep_folder)
    if not zip_path:
        return None
    if not _verify_episode_zip(ep_folder, zip_path):
        P.logger.warning('압축 검증 실패 — 원본 보존: %s', ep_folder)
        return None
    try:
        shutil.rmtree(ep_folder)
    except Exception as e:
        P.logger.warning('압축 후 폴더 삭제 실패 %s: %s', ep_folder, e)
    return zip_path


def _xml_escape(s) -> str:
    if s is None:
        return ''
    return (str(s).replace('&', '&amp;')
                  .replace('<', '"').replace('>', '"').strip())


# Kavita/Komga 호환 ComicInfo XML — reading_info 의 포맷과 동일
_INFO_XML = '''<?xml version="1.0"?>
<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Title>{title}</Title>
  <Series>{title}</Series>
  <Summary>{desc}</Summary>
  <Writer>{author}</Writer>
  <Publisher>{publisher}</Publisher>
  <Genre>{genre}</Genre>
  <Tags>{tags}</Tags>
  <LanguageISO>ko</LanguageISO>
  <Notes>{notes}</Notes>
  <CoverArtist></CoverArtist>
  <Penciller></Penciller>
  <Inker>{inker}</Inker>
  <Colorist></Colorist>
  <Letterer></Letterer>
  <Editor></Editor>
  <Characters></Characters>
  <Year>{year}</Year>
  <Month>{month}</Month>
  <Day>{day}</Day>
</ComicInfo>'''


# ---- 메타 헬퍼 (모듈 레벨 — auto/manual worker 모두에서 재사용) ----
def title_dir_for(download_root: str, title_name: str,
                  group: str = 'main', notice_subdir: str = '완결') -> str:
    c_folder = _safe_filename(title_name)
    if group == 'complete':
        return os.path.join(download_root,
                            _safe_filename(notice_subdir), c_folder)
    return os.path.join(download_root, c_folder)


def _title_search_key(name: str) -> str:
    """폴더명에서 검색용 제목 추출.

    다운로드 폴더명이 '제목 [작가 ／ 작가2]' 처럼 끝에 작가 블록이 붙는
    경우가 있어, 네이버 제목 검색 전에 끝의 ' [ ... ]' 한 덩어리를 제거한다.
    제거 결과가 비면 원본을 그대로 쓴다.
    """
    base = re.sub(r'\s*\[[^\[\]]*\]\s*$', '', name or '').strip()
    return base or (name or '').strip()


def _strip_leading_id_label(name: str) -> str:
    """'775141,제목' / '775141， 제목' 처럼 앞에 숫자ID+콤마 표기가 붙으면
    제목만 남긴다. (콤마는 작품 구분자가 아니므로 '검색용 후보' 생성에만 사용)
    """
    return re.sub(r'^\s*\d{4,}\s*[,，]\s*', '', name or '').strip()


def resolve_title_search(client, raw: str):
    """제목 문자열로 작품 검색. 원본이 안 잡히면 정리한 후보로 재시도.

    후보 순서: 원본 → 끝 작가블록 '[...]' 제거 → 앞 'ID,' 표기 제거 → 둘 다 제거.
    엉뚱한 작품을 받지 않도록 실제 매칭은 find_content(정확/공백·대소문자 일치)에
    위임한다. 어떤 후보도 정확히 일치하지 않으면 None.
    """
    base = _strip_leading_id_label(raw)
    cands: List[str] = []
    for c in (raw, _title_search_key(raw), base, _title_search_key(base)):
        c = (c or '').strip()
        if c and c not in cands:
            cands.append(c)
    for c in cands:
        it = client.find_content(c)
        if it:
            return it
    return None


def discover_title_folders(download_root: str,
                           notice_subdir: str = '완결',
                           logger=None) -> List[Tuple[str, str]]:
    """download_root 아래 실제로 존재하는 작품 폴더를 수집.

    - 최상위 디렉터리 = main 그룹 작품 폴더 (notice_subdir 폴더 자체는 제외)
    - notice_subdir(완결) 폴더 안의 하위 디렉터리 = complete 그룹 작품 폴더
    반환: [(group, folder_name), ...] (group ∈ 'main'|'complete'), 폴더명 정렬.

    메타 동기화 버튼이 watchlist(설정 'titles') 비어 있어도 동작하도록,
    디스크에 이미 받아둔 작품을 대상으로 삼기 위한 헬퍼.
    """
    found: List[Tuple[str, str]] = []
    seen = set()
    root = (download_root or '').strip()
    if not root or not os.path.isdir(root):
        return found
    notice = _safe_filename(notice_subdir or '완결')

    def _add(group: str, name: str) -> None:
        key = (group, name)
        if name and key not in seen:
            seen.add(key)
            found.append((group, name))

    try:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            if name == notice:
                # 완결 그룹 — 한 단계 더 내려가 작품 폴더 수집
                try:
                    for sub in sorted(os.listdir(full)):
                        if os.path.isdir(os.path.join(full, sub)):
                            _add('complete', sub)
                except OSError as e:
                    if logger:
                        logger.warning('[basic] 완결 폴더 스캔 실패: %s', e)
                continue
            _add('main', name)
    except OSError as e:
        if logger:
            logger.warning('[basic] 다운로드 폴더 스캔 실패: %s', e)
    return found


def build_info_xml(title_name: str, meta: Dict[str, Any],
                   articles: Optional[List[Dict[str, Any]]] = None) -> str:
    """네이버 작품 메타 → ComicInfo XML. 부족한 필드는 빈 값."""
    title = (meta.get('titleName') if meta else None) or title_name or ''
    synopsis = (meta.get('synopsis') if meta else '') or ''
    artists = (meta.get('communityArtists') if meta else None) or []
    writers = [a.get('name') or '' for a in artists
               if 'ARTIST_WRITER' in (a.get('artistTypeList') or [])]
    painters = [a.get('name') or '' for a in artists
                if 'ARTIST_PAINTER' in (a.get('artistTypeList') or [])]
    if not writers and artists:
        writers = [a.get('name') or '' for a in artists]
    tag_list = (meta.get('curationTagList') if meta else None) or []
    genres = [t.get('tagName') or '' for t in tag_list
              if str(t.get('curationType') or '').startswith('GENRE_')]
    tags = [t.get('tagName') or '' for t in tag_list
            if not str(t.get('curationType') or '').startswith('GENRE_')]
    finished = meta.get('finished') if meta else None
    notes = '완결' if finished else ('연재중' if finished is False else '')

    year = month = day = ''
    if articles:
        first = min(articles, key=lambda a: int(a.get('no') or 0))
        sdate = (first.get('serviceDateDescription') or '').strip()
        m = re.match(r'(\d{2})\.(\d{1,2})\.(\d{1,2})', sdate)
        if m:
            year = '20' + m.group(1)
            month = m.group(2).zfill(2)
            day = m.group(3).zfill(2)

    return _INFO_XML.format(
        title=_xml_escape(title),
        desc=_xml_escape(synopsis),
        author=_xml_escape(', '.join(w for w in writers if w)),
        inker=_xml_escape(', '.join(p for p in painters if p)),
        publisher=_xml_escape('네이버 웹툰'),
        genre=_xml_escape(', '.join(g for g in genres if g)),
        tags=_xml_escape(', '.join(t for t in tags if t)),
        notes=_xml_escape(notes),
        year=year, month=month, day=day,
    )


def ensure_title_metadata(client, download_root: str,
                          title_name: str, title_id: int,
                          meta: Dict[str, Any],
                          articles: Optional[List[Dict[str, Any]]] = None,
                          group: str = 'main',
                          notice_subdir: str = '완결') -> Dict[str, Any]:
    """작품 폴더에 info.xml / cover.jpg 가 없으면 생성.

    - 폴더가 없으면 만든다.
    - meta 가 부족해도 있는 정보만 채워서 info.xml 생성.
    - 커버 URL 이 없거나 client 가 없으면 cover.jpg 는 스킵.
    반환: {'info': bool, 'cover': bool, 'dir': str}
    """
    result = {'info': False, 'cover': False, 'dir': ''}
    title_dir = title_dir_for(download_root, title_name, group, notice_subdir)
    result['dir'] = title_dir
    try:
        os.makedirs(title_dir, exist_ok=True)
    except Exception as e:
        P.logger.warning('[%s] 작품 폴더 생성 실패: %s', title_name, e)
        return result

    info_path = os.path.join(title_dir, 'info.xml')
    if not os.path.exists(info_path):
        try:
            xml = build_info_xml(title_name, meta or {}, articles)
            with open(info_path, 'w', encoding='utf-8') as fp:
                fp.write(xml)
            P.logger.info('[%s] info.xml 생성', title_name)
            result['info'] = True
        except Exception as e:
            P.logger.warning('[%s] info.xml 생성 실패: %s', title_name, e)

    cover_path = os.path.join(title_dir, 'cover.jpg')
    if not os.path.exists(cover_path):
        url = ((meta or {}).get('sharedThumbnailUrl')
               or (meta or {}).get('posterThumbnailUrl')
               or (meta or {}).get('thumbnailUrl'))
        if url and client is not None:
            try:
                data = client.download_image(url, title_id)
                with open(cover_path, 'wb') as fp:
                    fp.write(data)
                P.logger.info('[%s] cover.jpg 생성', title_name)
                result['cover'] = True
            except Exception as e:
                P.logger.warning('[%s] cover.jpg 생성 실패: %s', title_name, e)
    return result


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


# ---- 전역 상호배제 락 ----
# 다운로드(자동/공지/수동)·압축·메타동기화가 절대 동시에 돌지 않게 한다.
# 한쪽이 회차 폴더를 zip+삭제(rmtree)하는 사이 다른 쪽이 같은 폴더에 쓰다가
# 폴더가 사라지는 사고(ENOENT 무더기)를 막는다. 09시 스케줄러 run() 에는 가드가
# 아예 없었고, 버튼 액션들은 click 시점의 status 만 봐서(check-then-act) 서로
# 겹칠 수 있었다 — 예: 실행이 긴 공지 다운로드 위에 09시 스케줄러가 또 run().
_run_lock = threading.Lock()


def try_acquire_run_lock() -> bool:
    """수동 워커 등 외부에서 전역 락을 비차단으로 잡는다. 성공 시 True."""
    return _run_lock.acquire(blocking=False)


def release_run_lock() -> None:
    try:
        _run_lock.release()
    except RuntimeError:
        pass


def _exclusive(fn):
    """전역 락을 잡고 메서드를 실행. 이미 다른 작업이 돌고 있으면 즉시 busy 반환.

    busy 일 때는 _auto_reset() 등으로 진행 중인 작업의 상태를 건드리지 않는다.
    """
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if not _run_lock.acquire(blocking=False):
            P.logger.info('[basic] %s skip — 다른 작업이 이미 실행 중', fn.__name__)
            return {'ret': 'fail', 'reason': 'busy', 'msg': '다른 작업 실행 중'}
        try:
            return fn(self, *args, **kwargs)
        finally:
            _run_lock.release()
    return wrapper


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
        self.proxy_url = NaverToonClient.resolve_proxy(
            self.cfg.get('use_proxy'), self.cfg.get('proxy_url'))
        self.use_compress = (self.cfg.get('use_compress') or 'False') == 'True'
        self.client: Optional[NaverToonClient] = None
        self.completed_items: List[Dict[str, Any]] = []  # 알림용 누적

    @staticmethod
    def _split_items(raw: str) -> List[str]:
        # 구분자: 개행 + 세로줄 계열(ASCII | / 전각 ｜ / │ / ¦). 전각 ｜ 로 붙여넣는
        # 경우가 많아 ASCII | 만 분리하면 한 덩어리로 뭉쳐 대부분 누락된다.
        out = []
        norm = re.sub(r'[|｜│¦]', '\n', (raw or '').replace('\r', ''))
        for chunk in norm.split('\n'):
            s = chunk.strip()
            if s:
                out.append(s)
        return out

    # ---- public ----
    @_exclusive
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
        # 공지 자동 다운(notice_auto_dl)만 쓰고 watchlist 가 비어있는 구성도 허용.
        # 이 경우 per-title 루프는 건너뛰고 아래 공지 처리까지 진행한다.
        if not self.items and not self.notice_auto_dl:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='체크할 작품 미설정')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = NaverToonClient(self.cookies_json, logger=P.logger,
                                          proxy_url=self.proxy_url)
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
        else:
            P.logger.info('[basic] notice_auto_dl Off — 공지 자동 다운 skip')

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
            it = resolve_title_search(self.client, raw)
            if not it:
                P.logger.warning('[%s] 검색 실패', raw)
                return 'failed'
            title_id = it['titleId']
            display = it.get('titleName') or raw
            P.logger.info('[%s] 검색→ title_id=%s title=%r', raw, title_id, display)

        return self._process_title(display, title_id)

    def _process_title(self, title: str, title_id: int) -> str:
        _auto_set(current_phase='fetch_episodes')
        meta: Dict[str, Any] = {}
        try:
            meta = self.client.get_content(title_id) or {}
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

        # info.xml / cover.jpg — 작품 폴더에 없으면 자동 생성 (다운로드 여부 무관)
        self._ensure_title_metadata(title, title_id, meta, articles, group='main')

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

    # ---- public: 공지만 실행 (UI '공지 즉시 실행' 버튼) ----
    @_exclusive
    def run_notice_only(self) -> dict:
        """공지 처리만 실행 — 메인 작품 다운로드 단계는 건너뜀.

        설정의 notice_auto_dl 토글 상태는 무시한다 (사용자가 명시적으로 클릭한
        실행이므로). last_paid_notice_id 가 최신 공지와 같으면 _process_paid_notices
        안에서 자체 skip — 중복 다운로드 안 함.
        """
        P.logger.info('[basic] Worker.run_notice_only BEGIN')
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='공지 처리 시작', titles_total=0)
        if not self.download_root:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.cookies_json:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='cookies_json 미설정')
            return {'ret': 'fail', 'reason': 'no_cookies'}

        try:
            self.client = NaverToonClient(self.cookies_json, logger=P.logger,
                                          proxy_url=self.proxy_url)
        except AuthRequiredError as e:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'쿠키 인증 실패: {e}')
            return {'ret': 'fail', 'reason': 'auth', 'msg': str(e)}

        if not self.client.verify():
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='쿠키 만료 — 재주입 필요')
            return {'ret': 'fail', 'reason': 'cookie_expired'}

        summary = {'titles': 0, 'downloaded': 0, 'skipped': 0, 'failed': 0}
        try:
            ncount = self._process_paid_notices(summary)
            P.logger.info('[basic] 공지 즉시 실행: %d 작품 추가 다운', ncount)
        except Exception as e:
            import traceback
            P.logger.error('공지 처리 예외: %s', e)
            P.logger.error(traceback.format_exc())

        # 다운로드 완료 요약 알림
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
                  message=(f"공지 완료 — 다운 {summary['downloaded']}, "
                           f"스킵 {summary['skipped']}, 실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ---- 유료화 공지 처리 ----
    def _process_paid_notices(self, summary: Dict[str, int]) -> int:
        """가장 최근 '유료화 전환' 공지에서 작품을 추출 → 무료 회차 다운.

        전환일이 이미 과거(<오늘)면 자동 스킵 (유료화돼서 무료 다운 불가).
        last_paid_notice_id 설정값으로 중복 처리 방지 — 같은 공지는 한 번만.
        """
        _auto_set(current_phase='fetch_notice', current_title='[유료화 공지]',
                  current_episode='', current_pages_done=0, current_pages_total=0)
        today = date.today()
        cands = self.client.find_paid_notices()
        if not cands:
            P.logger.info('[notice] 유료화 전환 공지 없음')
            return 0

        # "이번 달" 공지를 고른다 — 다음 달 공지가 먼저 올라와 있어도 가려지지 않게.
        latest = next((c for c in cands if c['year'] == today.year
                       and c['month'] == today.month), None)
        if latest is None:
            newest = cands[0]
            P.logger.info('[notice] 이번 달(%d년 %d월) 유료화 공지 없음 — skip '
                          '(가장 최근 공지 %d년 %d월)',
                          today.year, today.month,
                          newest['year'], newest['month'])
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
        failed_cnt = 0
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
                    failed_cnt += 1
            except Exception as e:
                P.logger.warning('[notice] %s 처리 실패: %s', title_name, e)
                summary['failed'] += 1; _auto_summary_inc('failed')
                failed_cnt += 1

        # 실패가 하나도 없을 때만 '처리 완료'로 기록 — 실패분은 다음 실행에서 재시도.
        # (이미 받은 회차는 DB 중복방지로 skip 되므로 재시도 비용은 낮다.)
        # 실패가 있는데도 기록해 버리면 같은 공지가 영구히 skip 되는 사고가 난다.
        if failed_cnt == 0:
            try:
                P.ModelSetting.set('last_paid_notice_id', str(latest['noticeId']))
                P.logger.info('[notice] last_paid_notice_id=%s 저장',
                              latest['noticeId'])
            except Exception as e:
                P.logger.warning('[notice] last_paid_notice_id 저장 실패: %s', e)
        else:
            P.logger.info('[notice] 실패 %d건 — last_paid_notice_id 미기록 '
                          '(다음 실행에서 재시도)', failed_cnt)
        return processed

    def _process_paid_title(self, title_id: int, title_name: str) -> str:
        """공지로 받은 작품 1개 — 메타 보강 후 무료 회차 모두 완결 폴더에 다운."""
        meta: Dict[str, Any] = {}
        try:
            meta = self.client.get_content(title_id) or {}
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

        self._ensure_title_metadata(title_name, title_id, meta, articles,
                                    group='complete')

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

    # ---- 작품 폴더 메타 (info.xml / cover.jpg) — Worker 인스턴스용 wrapper ----
    def _title_dir(self, title_name: str, group: str = 'main') -> str:
        return title_dir_for(self.download_root, title_name, group,
                             self.notice_subdir)

    def _ensure_title_metadata(self, title_name: str, title_id: int,
                               meta: Dict[str, Any],
                               articles: Optional[List[Dict[str, Any]]] = None,
                               group: str = 'main') -> Dict[str, Any]:
        return ensure_title_metadata(self.client, self.download_root,
                                     title_name, title_id, meta, articles,
                                     group=group,
                                     notice_subdir=self.notice_subdir)

    def _resolve_title_meta(self, *candidates) -> Tuple[Optional[int],
                                                        Dict[str, Any]]:
        """후보 문자열(URL/숫자ID/제목)들로 titleId + meta 해석.

        첫 성공을 반환. URL/숫자는 바로 titleId, 그 외엔 제목 검색.
        실패 시 (None, {}).
        """
        for cand in candidates:
            if not cand:
                continue
            tid = NaverToonClient.extract_title_id(cand)
            if tid is None:
                try:
                    it = self.client.find_content(cand)
                except NaverToonError as e:
                    P.logger.warning('[sync] find_content(%r) 실패: %s', cand, e)
                    it = None
                if not it:
                    continue
                tid = it.get('titleId')
            if tid is None:
                continue
            meta: Dict[str, Any] = {}
            try:
                meta = self.client.get_content(tid) or {}
            except NaverToonError as e:
                P.logger.warning('[%s] meta 실패: %s', cand, e)
            return tid, meta
        return None, {}

    def _episodes_safe(self, tid: int, label: str
                       ) -> Optional[List[Dict[str, Any]]]:
        """info.xml 날짜 산출용 회차 목록 — 실패해도 None 으로 진행."""
        try:
            return self.client.get_episodes_all(tid)
        except NaverToonError as e:
            P.logger.warning('[%s] 회차 조회 실패 (날짜 비움): %s', label, e)
            return None

    # ---- 전 작품 메타 일괄 동기화 (UI 버튼) ----
    @_exclusive
    def sync_metadata_all(self) -> dict:
        """다운로드 폴더의 모든 작품에 대해 info.xml/cover.jpg 누락분 생성.

        대상 = 설정 watchlist('titles') + download_path 를 스캔해 찾은 작품 폴더.
        → watchlist 가 비어 있어도 이미 받아둔 작품이 있으면 메타를 채운다.
        다운로드 폴더에 작품 폴더가 이미 있는 항목만 처리 (없는 작품은
        만들지 않고 스킵 — 다운로드 전에 굳이 빈 폴더 만들 필요 없음).
        완결 그룹(notice_subdir 아래) 도 동시에 점검.
        """
        # 대상 = 설정 watchlist(self.items) + 다운로드 폴더에 실제 존재하는 작품 폴더.
        # watchlist 가 비어 있어도 디스크에 받아둔 작품이 있으면 메타를 채운다.
        disk = discover_title_folders(self.download_root, self.notice_subdir,
                                      logger=P.logger)
        watchlist = list(self.items)
        # watchlist 에 이미 있는 폴더명은 디스크 목록에서 제외 (중복 처리 방지)
        wl_set = set(watchlist)
        disk = [(g, f) for (g, f) in disk if f not in wl_set]
        total = len(watchlist) + len(disk)

        P.logger.info('[basic] sync_metadata_all BEGIN watchlist=%s disk=%d '
                      'total=%d', watchlist, len(disk), total)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='메타 동기화 시작', titles_total=total)
        if not self.download_root:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정')
            return {'ret': 'fail', 'reason': 'no_download_path'}
        if not self.cookies_json:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='cookies_json 미설정')
            return {'ret': 'fail', 'reason': 'no_cookies'}
        if not total:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='동기화할 작품 없음 (watchlist 비어있고 다운로드 폴더에도 작품 폴더 없음)')
            return {'ret': 'fail', 'reason': 'no_titles'}

        try:
            self.client = NaverToonClient(self.cookies_json, logger=P.logger,
                                          proxy_url=self.proxy_url)
        except AuthRequiredError as e:
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message=f'쿠키 인증 실패: {e}')
            return {'ret': 'fail', 'reason': 'auth', 'msg': str(e)}

        summary = {'titles': total, 'info': 0, 'cover': 0,
                   'skipped_no_folder': 0, 'failed': 0}

        def _bump_done():
            _auto_set(titles_done=(summary['info'] + summary['cover']
                                   + summary['skipped_no_folder']
                                   + summary['failed']))

        # 1) 설정 watchlist — 해석된 제목으로 main/complete 폴더 점검
        for raw in watchlist:
            _auto_set(current_title=raw, current_phase='sync_metadata',
                      current_episode='', current_pages_done=0,
                      current_pages_total=0)
            try:
                tid, meta = self._resolve_title_meta(raw, _title_search_key(raw))
                if tid is None:
                    P.logger.warning('[sync_metadata] 제목 매칭 실패: %r', raw)
                    summary['failed'] += 1
                    _bump_done()
                    continue
                title_name = meta.get('titleName') or _title_search_key(raw) or raw
                _auto_set(current_title=title_name)

                # main / complete 두 위치 모두 점검 — 있는 쪽에만 메타 채움
                any_processed = False
                for group in ('main', 'complete'):
                    title_dir = self._title_dir(title_name, group=group)
                    if not os.path.isdir(title_dir):
                        continue
                    any_processed = True
                    articles = self._episodes_safe(tid, title_name)
                    r = self._ensure_title_metadata(title_name, tid, meta,
                                                   articles, group=group)
                    if r['info']:
                        summary['info'] += 1
                    if r['cover']:
                        summary['cover'] += 1
                if not any_processed:
                    summary['skipped_no_folder'] += 1
            except Exception as e:
                import traceback
                P.logger.error('[sync_metadata] %r 예외: %s', raw, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1
            _bump_done()

        # 2) 디스크에서 찾은 작품 폴더 — 폴더 그대로에 기록
        #    (폴더명 끝의 '[작가 …]' 블록을 떼고 제목 검색 → 실제 폴더에 info/cover)
        for group, folder in disk:
            _auto_set(current_title=folder, current_phase='sync_metadata',
                      current_episode='', current_pages_done=0,
                      current_pages_total=0)
            try:
                tid, meta = self._resolve_title_meta(_title_search_key(folder),
                                                     folder)
                if tid is None:
                    P.logger.warning('[sync_metadata] 제목 매칭 실패: %r', folder)
                    summary['failed'] += 1
                    _bump_done()
                    continue
                title_disp = meta.get('titleName') or folder
                _auto_set(current_title=title_disp)
                articles = self._episodes_safe(tid, title_disp)
                # 발견한 실제 폴더(folder)에 그대로 info.xml/cover.jpg 기록
                r = self._ensure_title_metadata(folder, tid, meta,
                                                articles, group=group)
                if r['info']:
                    summary['info'] += 1
                if r['cover']:
                    summary['cover'] += 1
            except Exception as e:
                import traceback
                P.logger.error('[sync_metadata] %r 예외: %s', folder, e)
                P.logger.error(traceback.format_exc())
                summary['failed'] += 1
            _bump_done()

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='', current_episode='',
                  message=(f"메타 동기화 완료 — info {summary['info']}, "
                           f"cover {summary['cover']}, "
                           f"폴더없음 {summary['skipped_no_folder']}, "
                           f"실패 {summary['failed']}"))
        return {'ret': 'success', **summary}

    # ---- 회차 폴더 일괄 압축 (UI 버튼) ----
    @_exclusive
    def compress_all(self) -> dict:
        """download_path 아래의 모든 회차 폴더를 ZIP 으로 압축.

        '회차 폴더' = 이미지 파일을 가진 leaf 폴더. 작품 폴더(info.xml/cover.jpg/
        .zip 보유)는 자동 제외. 이미 .zip 인 회차는 건너뜀 (멱등).

        2단계로 동작: (1) 후보를 모두 압축(zip 생성, 원본 유지) →
        (2) 각 zip 을 원본과 대조 검증 → 통과한 것만 원본 폴더 삭제.
        검증 실패 시 원본을 보존해 압축 사고로 인한 데이터 손실을 막는다.
        """
        P.logger.info('[basic] compress_all BEGIN root=%s', self.download_root)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='압축 시작')
        if not self.download_root or not os.path.isdir(self.download_root):
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정/없음')
            return {'ret': 'fail', 'reason': 'no_download_path'}

        # '회차 폴더' = 서브디렉토리 없는 leaf + 이미지 보유 폴더만 후보로 선정
        # (작품 폴더가 cover.jpg 만으로 잘못 후보에 들어가 통째로 zip+rmtree
        #  되는 사고 방지).
        candidates: List[str] = []
        for root, dirs, files in os.walk(self.download_root):
            if dirs:
                continue
            lower = [f.lower() for f in files]
            # 작품 폴더 신호(info.xml/cover.jpg/이미 압축된 .zip 회차)면 회차 폴더가
            # 아니므로 제외 — cover.jpg 때문에 작품 폴더가 통째로 압축되던 버그 방지.
            if ('info.xml' in lower or 'cover.jpg' in lower
                    or any(f.endswith(_ARCHIVE_EXTS) for f in lower)):
                continue
            if any(f.endswith(_IMAGE_EXTS) for f in lower):
                candidates.append(root)

        # Phase 1: 압축 — zip 만 생성하고 원본 폴더는 남겨둔다 (삭제는 검증 후).
        _auto_set(titles_total=len(candidates))
        zipped: List[tuple] = []   # (회차폴더, zip경로)
        compressed = 0
        skipped = 0
        failed = 0
        for idx, ep in enumerate(candidates, start=1):
            rel = os.path.relpath(ep, self.download_root)
            _auto_set(current_title=rel, current_phase='compressing',
                      titles_done=idx - 1)
            try:
                zip_path = _zip_episode_folder(ep)
                if zip_path:
                    zipped.append((ep, zip_path))
                    compressed += 1
                else:
                    skipped += 1
            except Exception as e:
                P.logger.warning('압축 예외 %s: %s', ep, e)
                failed += 1
            _auto_set(titles_done=idx)

        # Phase 2: 검증 후 원본 삭제 — 검증 통과한 회차만 폴더를 지운다.
        # 검증 실패 시 원본을 보존하고 손상된 zip 을 제거한다 (데이터 손실 방지).
        import shutil
        verified = 0
        verify_failed = 0
        for ep, zip_path in zipped:
            rel = os.path.relpath(ep, self.download_root)
            _auto_set(current_title=rel, current_phase='verifying')
            if _verify_episode_zip(ep, zip_path):
                try:
                    shutil.rmtree(ep)
                    verified += 1
                except Exception as e:
                    P.logger.warning('검증 후 폴더 삭제 실패 %s: %s', ep, e)
                    verify_failed += 1
            else:
                P.logger.warning('압축 검증 실패 — 원본 보존, 손상 zip 제거: %s', ep)
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
                verify_failed += 1

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='',
                  message=(f'압축 완료 — 생성 {compressed}개, 검증·삭제 {verified}개, '
                           f'검증실패(원본보존) {verify_failed}개, '
                           f'스킵 {skipped}개, 실패 {failed}개'))
        P.logger.info('[basic] compress_all END created=%d verified=%d '
                      'verify_failed=%d skipped=%d failed=%d',
                      compressed, verified, verify_failed, skipped, failed)
        return {'ret': 'success', 'processed': compressed,
                'verified': verified, 'verify_failed': verify_failed,
                'skipped': skipped, 'failed': failed}

    @_exclusive
    def add_pagecount_all(self) -> dict:
        """기존 압축 파일에 페이지수 #N 을 일괄 부여 (파일명만 변경, 멱등).

        download_path 아래 회차 아카이브(.zip/.cbz) 중 파일명 끝에 '#N' 이 없는
        것을 찾아, 내부 이미지 멤버 수 N 을 세고 '{stem}#{N}{ext}' 로 rename.
        이미 #N 있으면 skip; 이미지 0/열기 실패/대상 이름 존재 시 skip.
        DB(save_dir)는 항상 #N 없는 경로 정책이므로 손대지 않는다. 원본 삭제 없음.
        """
        P.logger.info('[basic] add_pagecount_all BEGIN root=%s', self.download_root)
        _auto_reset()
        _auto_set(status='running', started_at=datetime.now().isoformat(),
                  message='페이지수 부여 시작')
        if not self.download_root or not os.path.isdir(self.download_root):
            _auto_set(status='error', finished_at=datetime.now().isoformat(),
                      message='download_path 미설정/없음')
            return {'ret': 'fail', 'reason': 'no_download_path'}

        targets: List[str] = []
        for root, dirs, files in os.walk(self.download_root):
            for fn in files:
                if not fn.lower().endswith(_ARCHIVE_EXTS):
                    continue
                stem = os.path.splitext(fn)[0]
                if re.search(r'#\d+$', stem):
                    continue  # 이미 #N
                targets.append(os.path.join(root, fn))

        _auto_set(titles_total=len(targets))
        renamed = 0
        skipped = 0
        failed = 0
        for idx, path in enumerate(targets, start=1):
            rel = os.path.relpath(path, self.download_root)
            _auto_set(current_title=rel, current_phase='pagecount',
                      titles_done=idx - 1)
            try:
                n = _count_archive_images(path)
                if n <= 0:
                    skipped += 1
                else:
                    d = os.path.dirname(path)
                    stem, ext = os.path.splitext(os.path.basename(path))
                    new_path = os.path.join(d, f'{stem}#{n}{ext}')
                    if os.path.exists(new_path):
                        P.logger.warning('페이지수 부여 skip (대상 이름 존재): %s',
                                         new_path)
                        skipped += 1
                    else:
                        os.replace(path, new_path)
                        renamed += 1
                        P.logger.info('[pagecount] %s → #%d', rel, n)
            except Exception as e:
                P.logger.warning('페이지수 부여 실패 %s: %s', rel, e)
                failed += 1
            _auto_set(titles_done=idx)

        _auto_set(status='done', finished_at=datetime.now().isoformat(),
                  current_title='', current_phase='',
                  message=(f'페이지수 부여 완료 — 부여 {renamed}개, 스킵 {skipped}개, '
                           f'실패 {failed}개'))
        P.logger.info('[basic] add_pagecount_all END renamed=%d skipped=%d failed=%d',
                      renamed, skipped, failed)
        return {'ret': 'success', 'renamed': renamed,
                'skipped': skipped, 'failed': failed}

    # ---- one episode ----
    def _recognize_existing_zip(self, title_name: str, title_id: int,
                                no: int, subtitle: str, group: str) -> bool:
        """디스크에 회차 zip 이 이미 있으면 재다운 없이 DB에 completed 로 인식.

        압축(use_compress) 사용 시에만 동작 — zip 은 정상 완료된 회차에만 생성
        되므로 zip 존재 = 완전한 다운로드 보장. DB에 레코드가 없을 때만 호출.
        회차 식별은 (title_id, no) — subtitle 이 달라도 no 접두로 zip 을 찾는다.

        반환: 인식 처리했으면 True(호출측은 스킵), zip 없거나 압축 off 면 False.
        """
        if not self.use_compress:
            return False
        series_dir = title_dir_for(self.download_root, title_name,
                                   group, self.notice_subdir)
        zip_path = _find_episode_zip(series_dir, no)
        if not zip_path:
            return False
        rec = ModelNaverToonItem()
        rec.title_id = title_id
        rec.title_name = title_name
        rec.no = no
        rec.episode_title = subtitle
        rec.status = 'completed'
        rec.save_dir = _strip_pagecount(zip_path)
        rec.downloaded_at = datetime.now()
        rec.updated_time = rec.downloaded_at
        db.session.add(rec)
        db.session.commit()
        P.logger.info('[%s] %s(no=%s) 디스크 zip 존재 — 재다운 생략, DB 인식 (%s)',
                      title_name, subtitle, no, zip_path)
        return True

    def _download_one(self, title_name: str, title_id: int,
                      article: Dict[str, Any],
                      group: str = 'main') -> str:
        no = NaverToonClient.episode_no(article)
        subtitle = article.get('subtitle') or ''

        rec = (db.session.query(ModelNaverToonItem)
               .filter_by(title_id=title_id, no=no).first())
        if rec and rec.status == 'completed':
            return 'skipped'
        # DB엔 없지만 디스크에 이미 받은 zip 이 있으면 인식만 하고 스킵 (헛다운 방지)
        if rec is None and self._recognize_existing_zip(
                title_name, title_id, no, subtitle, group):
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

        # ---- 저장 경로 ---- (디스크 확인과 같은 series 폴더 규칙: title_dir_for)
        e_folder = f'{no:04d}_{_safe_filename(subtitle)}'
        save_dir = os.path.join(
            title_dir_for(self.download_root, title_name, group, self.notice_subdir),
            e_folder)
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

        # 정상 완료 + 압축 옵션 On → 회차 폴더 ZIP 압축
        if self.use_compress and rec.status == 'completed':
            zip_path = compress_episode_folder(save_dir)
            if zip_path:
                rec.save_dir = _strip_pagecount(zip_path)
                db.session.commit()
                P.logger.info('[%s] %s 압축 완료 → %s',
                              title_name, subtitle, zip_path)

        return 'downloaded' if rec.status in ('completed', 'partial') else 'failed'
