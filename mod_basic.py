import threading
import time
import traceback

from .model import ModelNaverToonItem
from .setup import *
from .worker import Worker


class ModuleBasic(PluginModuleBase):

    def __init__(self, P):
        super(ModuleBasic, self).__init__(
            P, name='basic', first_menu='setting',
            scheduler_desc='네이버웹툰 자동 다운로드',
        )
        self.db_default = {
            f'db_version': '1',
            f'{self.name}_auto_start': 'False',
            # 매월 1일 09:00 — 유료화 공지 catch-up + 신규 무료 회차 점검
            f'{self.name}_interval': '0 9 1 * *',
            f'{self.name}_db_delete_day': '90',
            f'{self.name}_db_auto_delete': 'False',
            f'{P.package_name}_item_last_list_option': '',

            'titles': '',
            'cookies_json': '',
            'download_path': '',
            'notify_webhook_cookie': '',       # 쿠키 만료 시 발송할 웹훅
            'notify_webhook_download': '',     # 다운로드 완료 요약 발송 웹훅
            'cookie_expired_notified': 'False',# 쿠키 만료 알림 1회 발송 플래그
            'notice_auto_dl': 'False',         # 매월 유료화 공지 자동 다운로드
            'notice_subdir': '완결',            # 완결/유료화 작품 저장 하위 폴더
            'last_paid_notice_id': '0',        # 마지막 처리한 유료화 공지 noticeId
            'auto_start': 'False',
        }
        self.web_list_model = ModelNaverToonItem

    def plugin_load(self):
        """SJVA 시작 시 호출 — 유료화 공지 catch-up 트리거.

        서비스가 내려가있어서 매월 1일 스케줄을 못 돌린 경우, 시작 직후 한 번
        체크해서 미처리 공지가 있으면 처리한다. (이번 달 + 미처리 공지만)
        """
        P.logger.info('[basic] plugin_load — 유료화 공지 catch-up 예약')
        threading.Thread(target=self._startup_catch_up, daemon=True).start()

    @staticmethod
    def _bind_ready() -> bool:
        """Flask-SQLAlchemy 3.x 의 db.engines 에 우리 bind 가 잡혔는지.

        flask_sqlalchemy/session.py:get_bind 가 실제로 보는 곳이 db.engines.
        app.config['SQLALCHEMY_BINDS'] 에 URI 만 박혀있고 engine 이 lazy
        등록 안 된 상태에서는 ModelSetting.get 이 framework 내부에서 실패한다.
        """
        try:
            eng = db.engines.get(P.package_name)
            return eng is not None
        except Exception:
            return False

    @staticmethod
    def _wait_for_bind(timeout: int = 300, interval: int = 5) -> bool:
        import time as _time
        end = _time.time() + timeout
        while _time.time() < end:
            if ModuleBasic._bind_ready():
                return True
            _time.sleep(interval)
        return False

    def _startup_catch_up(self):
        """SJVA bind 등록 완료를 폴링한 뒤 catch-up. bind 가 끝내 안 잡히면
        조용히 종료 — 등록된 스케줄러 tick (매월 1일) 이 다음 기회에 처리.
        """
        time.sleep(10)
        ready = self._wait_for_bind(timeout=300, interval=5)
        # 진단 로그 — 등록 상태 가시화
        try:
            cfg_binds = list((F.app.config.get('SQLALCHEMY_BINDS') or {}).keys())
        except Exception:
            cfg_binds = ['<?>']
        try:
            engines = list(getattr(db, 'engines', {}) or {})
        except Exception:
            engines = ['<?>']
        P.logger.info('[basic] catch-up bind 상태: ready=%s cfg_binds=%s engines=%s',
                      ready, cfg_binds, engines)
        if not ready:
            P.logger.info('[basic] catch-up: bind 등록 안 됨 — skip '
                          '(다음 스케줄 tick 이 처리)')
            return
        try:
            with F.app.app_context():
                flag = P.ModelSetting.get('notice_auto_dl')
                if (flag or 'False') != 'True':
                    P.logger.info('[basic] catch-up: notice_auto_dl Off — skip')
                    return
                P.logger.info('[basic] catch-up: Worker 실행')
                w = Worker()
                w.run()
        except Exception as e:
            P.logger.warning('[basic] catch-up 예외(다음 스케줄 tick 에 처리됨): %s', e)
            P.logger.warning(traceback.format_exc())

    def process_menu(self, sub, req):
        arg = P.ModelSetting.to_dict()
        if sub == 'setting':
            arg['is_include'] = F.scheduler.is_include(self.get_scheduler_name())
            arg['is_running'] = F.scheduler.is_running(self.get_scheduler_name())
        return render_template(f'{P.package_name}_{self.name}_{sub}.html', arg=arg)

    def process_command(self, command, arg1=None, arg2=None, arg3=None, req=None):
        try:
            P.logger.info('[basic.process_command] cmd=%r arg1=%r arg2=%r arg3=%r',
                          command, arg1, arg2, arg3)
        except Exception:
            pass
        ret = {'ret': 'success'}
        try:
            if command == 'verify_cookies':
                from .client import NaverToonClient, AuthRequiredError
                try:
                    cli = NaverToonClient(P.ModelSetting.get('cookies_json'), logger=P.logger)
                    ok = cli.verify()
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '쿠키 유효 (로그인 상태 확인됨)' if ok else '쿠키 만료/무효 — 재주입 필요'}
                except AuthRequiredError as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'run_now':
                ret = self.do_action()
            elif command == 'mrun':
                from . import manual_worker
                url = (arg1 or '').strip()
                if not url and req is not None:
                    try:
                        url = (req.form.get('url') or req.values.get('url')
                               or req.args.get('url') or '').strip()
                    except Exception:
                        pass
                ret = manual_worker.run_with_url(url)
            elif command == 'mcancel':
                from . import manual_worker
                manual_worker.cancel()
                ret = {'ret': 'success', 'msg': '취소 요청 보냄'}
            elif command == 'mprogress':
                from . import manual_worker
                ret = {'ret': 'success', 'state': manual_worker.get_state()}
            elif command == 'status_progress':
                from . import manual_worker, worker as auto_worker
                ret = {
                    'ret': 'success',
                    'auto': auto_worker.get_auto_state(),
                    'manual': manual_worker.get_state(),
                }
            elif command == 'notify_test':
                # arg1 = 'cookie' | 'download'
                from .notify import send_webhook
                kind = (arg1 or 'cookie').strip().lower()
                url_key = ('notify_webhook_cookie' if kind == 'cookie'
                           else 'notify_webhook_download')
                url = (P.ModelSetting.get(url_key) or '').strip()
                if not url:
                    ret = {'ret': 'fail', 'msg': f'{kind} URL 미설정'}
                else:
                    msg = f'[네이버웹툰] 테스트 알림 ({kind}) — 정상 수신 확인용'
                    ok = send_webhook(url, msg)
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '발송 성공' if ok else '발송 실패 (URL/형식 확인)'}
            elif command == 'db_delete_items':
                ids = []
                for x in (arg1 or '').split(','):
                    x = x.strip()
                    if x.isdigit():
                        ids.append(int(x))
                if not ids:
                    ret = {'ret': 'fail', 'msg': '삭제할 ID 없음', 'count': 0}
                else:
                    cnt = (db.session.query(ModelNaverToonItem)
                           .filter(ModelNaverToonItem.id.in_(ids))
                           .delete(synchronize_session=False))
                    db.session.commit()
                    ret = {'ret': 'success', 'count': cnt}
        except Exception as e:
            P.logger.error('[basic.process_command] inner Exception: %s', e)
            P.logger.error(traceback.format_exc())
            ret = {'ret': 'fail', 'msg': str(e)}
        try:
            return jsonify(ret)
        except Exception as e:
            P.logger.error('[basic.process_command] jsonify 실패: %s ret=%r', e, ret)
            return jsonify({'ret': 'fail', 'msg': f'jsonify 실패: {e}'})

    def scheduler_function(self):
        P.logger.info('[basic] scheduler_function CALLED')
        try:
            ret = self.do_action()
            P.logger.info('[basic] scheduler 종료: %s', ret)
        except Exception as e:
            P.logger.error('[basic] scheduler Exception: %s', e)
            P.logger.error(traceback.format_exc())

    def do_action(self):
        P.logger.info('[basic] do_action BEGIN')
        try:
            with F.app.app_context():
                w = Worker()
                ret = w.run()
                P.logger.info('[basic] do_action END ret=%s', ret)
                return ret
        except Exception as e:
            P.logger.error('[basic] do_action Exception: %s', e)
            P.logger.error(traceback.format_exc())
            return {'ret': 'fail', 'msg': str(e)}
