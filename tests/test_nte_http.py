import json
import logging
import os
import tempfile
import traceback
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import nte


def make_response(payload=None, *, text=None, status=200):
    response = nte.requests.Response()
    response.status_code = status
    response._content = (json.dumps(payload) if text is None else text).encode('utf-8')
    response.encoding = 'utf-8'
    return response


class NteHttpTests(unittest.TestCase):
    def test_ds_matches_public_protocol_test_vector(self):
        # Reference fixture, independent of this module's default app version:
        # zzstar101/taygedo-auto-attendance/test/protocol.test.ts
        with patch.object(nte.time, 'time', return_value=1710000000), \
                patch.object(nte.secrets, 'choice', side_effect=list('ABCDEFGH')):
            self.assertEqual(
                nte.generate_ds('1.2.2'),
                '1710000000,ABCDEFGH,6cf4e2edb3dc484539a2b8d90080c2db',
            )

    def test_signature_uses_case_insensitive_appversion_without_mutating_headers(self):
        original = {'AppVersion': '1.2.2', 'Authorization': 'fake-access'}
        with patch.object(nte.requests, 'get', return_value=make_response({'code': 0})) as get, \
                patch.object(nte.time, 'time', return_value=1710000000), \
                patch.object(nte.secrets, 'choice', side_effect=list('ABCDEFGH')):
            nte._request_get(nte.GAME_SIGNIN_STATE_URL, headers=original)
        sent = get.call_args.kwargs
        self.assertEqual(sent['headers']['ds'],
                         '1710000000,ABCDEFGH,6cf4e2edb3dc484539a2b8d90080c2db')
        self.assertEqual(sent['headers']['appversion'], '1.2.2')
        self.assertEqual(original, {'AppVersion': '1.2.2', 'Authorization': 'fake-access'})
        self.assertEqual(sent['timeout'], (10, 30))
        self.assertFalse(sent['allow_redirects'])

    def test_sms_login_refresh_and_every_sign_endpoint_are_signed(self):
        responses = {
            nte.CHECK_CAPTCHA_URL: {'code': 0},
            nte.LOGIN_URL: {'code': 0, 'result': {'token': 'fake-sdk', 'userId': 'test-sdk-user'}},
            nte.USER_CENTER_LOGIN_URL: {'code': 0, 'data': {
                'accessToken': 'fake-access', 'refreshToken': 'fake-refresh', 'uid': 'test-user',
            }},
            nte.REFRESH_TOKEN_URL: {'code': 0, 'data': {
                'accessToken': 'fake-next-access', 'refreshToken': 'fake-next-refresh',
            }},
            nte.GET_GAME_ROLES_URL: {'code': 0, 'data': {
                'roles': [{'roleId': 'test-role', 'roleName': 'Test'}],
            }},
            nte.APP_SIGNIN_URL: {'code': 0, 'data': {'exp': 1, 'goldCoin': 2}},
            nte.GAME_SIGNIN_URL: {'code': 0},
            nte.GAME_SIGNIN_STATE_URL: {'code': 0, 'data': {'todaySign': True, 'days': 1}},
            nte.GAME_SIGN_REWARDS_URL: {'code': 0, 'data': [{'name': 'Test item', 'num': 3}]},
        }
        calls = []

        def respond(url, **kwargs):
            calls.append((url, kwargs))
            return make_response(responses[url])

        with patch.object(nte.requests, 'get', side_effect=respond), \
                patch.object(nte.requests, 'post', side_effect=respond), \
                patch.object(nte, 'sign_game_ids_env', None):
            account = nte.build_account_by_sms('13800000000', '000000', 'test-device')
            self.assertEqual(account['roleIds'], ['test-role'])
            output = []
            with patch('builtins.print', side_effect=AssertionError('Unexpected global stdout output')):
                self.assertTrue(nte.do_sign(account, output=output.append))
            self.assertEqual(account['refreshToken'], 'fake-next-refresh')
            self.assertEqual(len(output), 2)
            self.assertIn('社区签到成功', output[0])
            self.assertIn('角色Test(test-role)签到成功', output[1])

        bbs_calls = [(url, kwargs) for url, kwargs in calls if 'bbs-api.tajiduo.com/' in url]
        self.assertEqual({url for url, _ in bbs_calls}, {
            nte.USER_CENTER_LOGIN_URL, nte.REFRESH_TOKEN_URL, nte.GET_GAME_ROLES_URL,
            nte.APP_SIGNIN_URL, nte.GAME_SIGNIN_URL, nte.GAME_SIGNIN_STATE_URL,
            nte.GAME_SIGN_REWARDS_URL,
        })
        for url, kwargs in calls:
            with self.subTest(url=url):
                self.assertEqual(kwargs['timeout'], (10, 30))
                if 'bbs-api.tajiduo.com/' in url:
                    self.assertRegex(kwargs['headers']['ds'], r'^\d+,[A-Za-z0-9]{8},[0-9a-f]{32}$')
                    self.assertEqual(kwargs['headers']['appversion'], nte.APPVERSION)
                else:
                    self.assertNotIn('ds', kwargs['headers'])

    def test_laohu_requests_have_timeouts_and_no_ds(self):
        calls = []

        def respond(url, **kwargs):
            calls.append((url, kwargs))
            return make_response({'code': 0, 'result': {
                'token': 'fake-cloud', 'userId': 'test-cloud-user', 'count': 2,
            }})

        account = {
            'cloudToken': 'fake-cloud', 'cloudUserId': 'test-cloud-user', 'deviceId': 'test-device',
        }
        with patch.object(nte.requests, 'get', side_effect=respond), \
                patch.object(nte.requests, 'post', side_effect=respond):
            nte.send_captcha('13800000000', 'test-device')
            nte.query_cloud_whether_set_password('13800000000', 'test-device')
            nte.send_cloud_captcha('13800000000', 'test-device')
            nte.cloud_login('13800000000', '000000', 'test-device')
            nte.login_with_password('13800000000', 'fake-password', 'test-device')
            nte.cloud_get_user_info(account)
            self.assertEqual(nte.cloud_untreated_count(account), 2)
            nte._request_json(nte.LOGIN_URL, {'token': 'fake'}, nte.REQUEST_HEADERS_BASE)
        self.assertEqual(len(calls), 8)
        for url, kwargs in calls:
            with self.subTest(url=url):
                self.assertEqual(kwargs['timeout'], (10, 30))
                self.assertNotIn('ds', kwargs['headers'])

    def test_similarly_named_host_does_not_receive_signature(self):
        with patch.object(nte.requests, 'get', return_value=make_response({'code': 0})) as get:
            nte._request_get('https://bbs-api.tajiduo.com.example.com/test')
        self.assertNotIn('ds', get.call_args.kwargs['headers'])

    def test_non_json_empty_and_non_object_responses_do_not_leak_body(self):
        for text in ('sdk-token-secret <html>userId=private</html>', '   ', '["sdk-token-secret"]'):
            with self.subTest(text=text):
                with self.assertRaises(Exception) as raised:
                    nte._safe_json(make_response(text=text, status=502), '测试接口')
                detail = ''.join(traceback.format_exception(raised.exception))
                self.assertNotIn('sdk-token-secret', detail)
                self.assertNotIn('userId=private', detail)
                self.assertIn('status=502', str(raised.exception))

    def test_missing_fields_and_error_without_message_do_not_echo_payload(self):
        for payload in ({'code': 0, 'data': {'accessToken': 'sdk-token-secret'}},
                        {'code': 22, 'data': {'token': 'sdk-token-secret', 'userId': 'private'}}):
            with self.subTest(payload=payload):
                with patch.object(nte.requests, 'post', return_value=make_response(payload)), \
                        self.assertRaises(Exception) as raised:
                    nte.user_center_login('fake-sdk', 'test-user', 'test-device')
                self.assertNotIn('sdk-token-secret', str(raised.exception))
                self.assertNotIn('private', str(raised.exception))

    def test_transport_errors_do_not_echo_url_or_credentials(self):
        for error in (nte.requests.Timeout('token=secret-in-url'),
                      nte.requests.ConnectionError('https://test/?token=secret-in-url')):
            with self.subTest(error=type(error).__name__):
                with patch.object(nte.requests, 'get', side_effect=error), \
                        self.assertRaises(Exception) as raised:
                    nte._request_get(nte.GAME_SIGNIN_STATE_URL)
                self.assertNotIn('secret-in-url', ''.join(traceback.format_exception(raised.exception)))

    def test_no_implicit_cross_game_fallback(self):
        with patch.object(nte, 'sign_game_ids_env', None):
            self.assertEqual(nte._candidate_sign_game_ids('1289'), ['1289'])
            self.assertEqual(nte._candidate_sign_game_ids('custom-game'), ['custom-game'])
        with patch.object(nte, 'sign_game_ids_env', '1257,1289,1257'):
            self.assertEqual(nte._candidate_sign_game_ids('1289'), ['1289', '1257'])

    def test_failed_game_sign_does_not_try_another_game(self):
        with patch.object(nte, 'sign_game_ids_env', None), \
                patch.object(nte.requests, 'post', return_value=make_response({'code': 1, 'msg': '失败'})) as post:
            self.assertFalse(nte.game_signin('fake-access', 'test-role', '1289')[0])
        self.assertEqual(post.call_count, 1)
        self.assertIn('gameId=1289', post.call_args.kwargs['data'])


class NteLoggerTests(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = os.getcwd()
        self.directory = tempfile.TemporaryDirectory()
        self.root_logger = logging.getLogger()
        self.previous_level = self.root_logger.level
        self.previous_handlers = list(self.root_logger.handlers)
        os.chdir(self.directory.name)

    def tearDown(self):
        for handler in list(self.root_logger.handlers):
            if handler not in self.previous_handlers:
                self.root_logger.removeHandler(handler)
                handler.close()
        self.root_logger.setLevel(self.previous_level)
        os.chdir(self.previous_cwd)
        self.directory.cleanup()

    def test_existing_log_and_repeated_configuration_are_safe_and_private(self):
        Path('logs').mkdir()
        log_path = Path('logs') / f'{date.today().isoformat()}.log'
        log_path.write_text('existing log\n', encoding='utf-8')
        before_get, before_post = nte.requests.get, nte.requests.post
        nte.config_logger()
        nte.config_logger()
        managed = [h for h in self.root_logger.handlers if getattr(h, '_nte_log_handler', False)]
        self.assertEqual(len(managed), 1)
        self.assertIs(nte.requests.get, before_get)
        self.assertIs(nte.requests.post, before_post)
        with patch.object(nte.requests, 'get', return_value=make_response(text='raw-body-secret')):
            nte._request_get(nte.LOGIN_URL + '?token=query-secret', headers={'Authorization': 'header-secret'})
        managed[0].flush()
        content = log_path.read_text(encoding='utf-8')
        self.assertIn('existing log', content)
        self.assertEqual(content.count('GET user.laohu.com/openApi/sms/new/login status=200'), 1)
        for secret in ('raw-body-secret', 'query-secret', 'header-secret'):
            self.assertNotIn(secret, content)

    def test_rollover_closes_previous_owned_handler(self):
        fake_date = Mock()
        fake_date.today.side_effect = [date(2030, 1, 1), date(2030, 1, 2)]
        with patch.object(nte, 'date', fake_date):
            nte.config_logger()
            old_handler = next(h for h in self.root_logger.handlers if getattr(h, '_nte_log_handler', False))
            nte.config_logger()
        self.assertIsNone(old_handler.stream)
        self.assertNotIn(old_handler, self.root_logger.handlers)
        self.assertEqual(len([h for h in self.root_logger.handlers if getattr(h, '_nte_log_handler', False)]), 1)


if __name__ == '__main__':
    unittest.main()
