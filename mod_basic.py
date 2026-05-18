import threading
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
            # 매일 09:00 — 신규 무료 회차 + 이번 달 유료화 공지 자동 catch-up
            f'{self.name}_interval': '0 9 * * *',
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
            'use_proxy': 'False',              # 프록시 사용 여부
            'proxy_url': '',                   # warproxy 등. use_proxy=True + 값 있을 때만 사용
            'auto_start': 'False',
        }
        self.web_list_model = ModelNaverToonItem

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
                    proxy_url = NaverToonClient.resolve_proxy(
                        P.ModelSetting.get('use_proxy'),
                        P.ModelSetting.get('proxy_url'))
                    cli = NaverToonClient(P.ModelSetting.get('cookies_json'),
                                          logger=P.logger, proxy_url=proxy_url)
                    ok = cli.verify()
                    ret = {'ret': 'success' if ok else 'fail',
                           'msg': '쿠키 유효 (로그인 상태 확인됨)' if ok else '쿠키 만료/무효 — 재주입 필요'}
                except AuthRequiredError as e:
                    ret = {'ret': 'fail', 'msg': str(e)}
            elif command == 'run_now':
                ret = self.do_action()
            elif command == 'run_notice_now':
                ret = self.do_action_notice_only()
            elif command == 'sync_metadata':
                ret = self.do_action_sync_metadata()
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

    def do_action_notice_only(self):
        """공지만 즉시 실행 — HTTP 요청에서 즉시 응답하고 백그라운드에서 처리.

        오래 걸릴 수 있으니(작품 27개 × 다수 회차) 동기 실행하면 timeout.
        진행 상황은 worker._auto_state 로 노출되며 '진행 상황' 메뉴에서 확인.
        """
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 자동 다운로드 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    w = Worker()
                    w.run_notice_only()
            except Exception as e:
                P.logger.error('[basic] notice-only run Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '공지 기반 다운로드 시작됨 — "진행 상황" 메뉴에서 확인'}

    def do_action_sync_metadata(self):
        """체크할 작품 전체의 info.xml / cover.jpg 누락분 백그라운드 동기화."""
        from . import worker as auto_worker
        if auto_worker.get_auto_state().get('status') == 'running':
            return {'ret': 'fail', 'msg': '이미 자동 다운로드 실행 중'}

        def _bg():
            try:
                with F.app.app_context():
                    Worker().sync_metadata_all()
            except Exception as e:
                P.logger.error('[basic] sync_metadata run Exception: %s', e)
                P.logger.error(traceback.format_exc())

        threading.Thread(target=_bg, daemon=True).start()
        return {'ret': 'success',
                'msg': '메타 동기화 시작됨 — "진행 상황" 메뉴에서 확인'}
