"""Regression tests for pending private-message login sessions.

Run with: python -m unittest discover -s tests -v
Every account API is mocked; these tests never send SMS or account requests.
"""

import asyncio
import copy
from datetime import datetime, timedelta
import time
import unittest
from unittest.mock import patch

try:
    from .support import MessageEvent, collect, load_plugin_module
except ImportError:
    from support import MessageEvent, collect, load_plugin_module


main = load_plugin_module()
PHONE = "13800138000"
OTHER_PHONE = "13900139000"
ACCOUNT = {"uid": "tgd-user", "refreshToken": "refresh", "gameId": "1289"}
CLOUD_ACCOUNT = {"cloudUserId": "cloud-user", "cloudToken": "cloud-token"}
LOGIN_API = {
    "password": "build_account_by_password",
    "sms": "build_account_by_sms",
    "cloud_sms": "build_cloud_account_by_sms",
}
REQUEST_COMMAND = {
    "sms": ("nteph", "send_login_captcha"),
    "cloud_sms": ("nteyun", "send_cloud_login_captcha"),
}


class FrozenDateTime(datetime):
    current = datetime(2026, 10, 5, 10, 0, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.current


class PendingLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = main.NTEPlugin(object(), {})
        self.event = MessageEvent("123456")
        self.keys = self.plugin._build_user_keys(self.event)

    async def pending(self, mode="sms", *, key=None, phone=PHONE):
        await self.plugin._set_pending(
            key or self.keys[0],
            {"mode": mode, "phone": phone, "device_id": "device"},
        )
        return copy.deepcopy(self.plugin.storage["pending_login"][key or self.keys[0]])

    async def handle(self, text, **event_options):
        return await collect(
            self.plugin.handle_pending_login_input(MessageEvent(text, **event_options))
        )

    def pending_keys(self):
        return set(self.plugin.storage.get("pending_login", {}))

    def assert_relogin_hint(self, messages):
        message = "\n".join(messages)
        self.assertIn("登录失败", message)
        self.assertIn("重新", message)
        self.assertNotIn("可直接重试", message)

    async def test_ordinary_chat_and_non_ascii_or_wrong_length_codes_pass_silently(self):
        # Issue #1: none of these messages may reach either SMS login API.
        for mode in ("sms", "cloud_sms"):
            for text in ("你好", "普通聊天", "123", "123456789", "１２３４５６", "١٢٣٤٥٦", "123 456", "12x456", "", "   "):
                with self.subTest(mode=mode, text=text):
                    await self.pending(mode)
                    before = copy.deepcopy(self.plugin.storage)
                    with patch.object(main.nte, LOGIN_API[mode], side_effect=AssertionError("Unexpected login attempt")) as login:
                        self.assertEqual([], await self.handle(text))
                    login.assert_not_called()
                    self.assertEqual(before, self.plugin.storage)

    async def test_ascii_codes_accept_four_to_eight_digits_after_strip(self):
        for mode in ("sms", "cloud_sms"):
            for text in ("1234", "12345", " 123456\n", "1234567", "12345678"):
                with self.subTest(mode=mode, text=text):
                    await self.pending(mode)
                    account = CLOUD_ACCOUNT if mode == "cloud_sms" else ACCOUNT
                    with patch.object(main.nte, LOGIN_API[mode], return_value=copy.deepcopy(account)) as login:
                        messages = await self.handle(text)
                    login.assert_called_once_with(PHONE, text.strip(), "device")
                    self.assertTrue(any("登录成功" in message for message in messages))
                    self.assertFalse(self.pending_keys())

    async def test_failed_login_consumes_session_and_chat_stays_silent(self):
        # Issue #1: a rejected code must stop the unwanted "登录失败" replies.
        for mode in LOGIN_API:
            with self.subTest(mode=mode):
                session = await self.pending(mode)
                self.plugin.storage["pending_login"].update(
                    {key: copy.deepcopy(session) for key in self.keys}
                )
                self.plugin.storage["pending_login"]["test:99"] = copy.deepcopy(session)
                with patch.object(main.nte, LOGIN_API[mode], side_effect=RuntimeError("凭据无效")) as login:
                    messages = await self.handle("123456")
                    self.assert_relogin_hint(messages)
                    self.assertEqual({"test:99"}, self.pending_keys())
                    self.assertEqual([], await self.handle("后续聊天"))
                    self.assertEqual([], await self.handle("123456"))
                login.assert_called_once()

    async def test_session_is_consumed_before_the_login_api_is_called(self):
        for mode in LOGIN_API:
            with self.subTest(mode=mode):
                await self.pending(mode)
                observed_pending = []

                def login(*args):
                    observed_pending.append(self.pending_keys())
                    return copy.deepcopy(ACCOUNT)

                with patch.object(main.nte, LOGIN_API[mode], side_effect=login):
                    messages = await self.handle("123456")
                self.assertEqual([set()], observed_pending)
                self.assertTrue(any("登录成功" in message for message in messages))

    async def test_commands_are_never_treated_as_credentials(self):
        commands = [
            "nte", "nte 1", "ntepw 13800138000", "nteph 13800138000",
            "nteyun 13800138000", "ntelist", "ntelogout 1", "ntehelp",
            "ntecancel", "NTEHELP", "/other_plugin", "/123456", " /help ",
        ]
        for mode in LOGIN_API:
            for text in commands:
                with self.subTest(mode=mode, text=text):
                    await self.pending(mode)
                    before = copy.deepcopy(self.plugin.storage)
                    with patch.object(main.nte, LOGIN_API[mode], side_effect=AssertionError("Unexpected login attempt")) as login:
                        self.assertEqual([], await self.handle(text))
                    login.assert_not_called()
                    self.assertEqual(before, self.plugin.storage)

    async def test_password_can_contain_normal_text(self):
        await self.pending("password")
        with patch.object(main.nte, "build_account_by_password", return_value=copy.deepcopy(ACCOUNT)) as login:
            messages = await self.handle("my real password")
        login.assert_called_once_with(PHONE, "my real password")
        self.assertTrue(any("登录成功" in message for message in messages))

    async def test_expired_and_missing_timestamps_clear_only_this_users_pending(self):
        for timestamp in (None, 0, "invalid timestamp", int(time.time()) - main.PENDING_EXPIRE_SECONDS - 5):
            with self.subTest(timestamp=timestamp):
                await self.pending()
                if timestamp is None:
                    self.plugin.storage["pending_login"][self.keys[0]].pop("created_at")
                else:
                    self.plugin.storage["pending_login"][self.keys[0]]["created_at"] = timestamp
                self.plugin.storage["pending_login"]["test:99"] = {"mode": "sms", "created_at": int(time.time())}
                with patch.object(main.nte, "build_account_by_sms") as login:
                    messages = await self.handle("123456")
                login.assert_not_called()
                self.assertTrue(any("过期" in message for message in messages))
                self.assertEqual({"test:99"}, self.pending_keys())

    async def test_expired_sms_session_does_not_reply_to_ordinary_chat(self):
        for mode in ("sms", "cloud_sms"):
            with self.subTest(mode=mode):
                await self.pending(mode)
                self.plugin.storage["pending_login"][self.keys[0]]["created_at"] = 0
                with patch.object(main.nte, LOGIN_API[mode]) as login:
                    messages = await self.handle("继续聊天")
                login.assert_not_called()
                self.assertEqual([], messages)
                self.assertFalse(self.pending_keys())

    async def test_sms_session_is_valid_at_the_ttl_boundary(self):
        FrozenDateTime.current = datetime(2026, 10, 5, 10, 0, 0)
        with patch.object(main, "datetime", FrozenDateTime):
            await self.pending()
            FrozenDateTime.current += timedelta(seconds=main.PENDING_EXPIRE_SECONDS)
            with patch.object(main.nte, "build_account_by_sms", return_value=copy.deepcopy(ACCOUNT)) as login:
                messages = await self.handle("123456")
        login.assert_called_once_with(PHONE, "123456", "device")
        self.assertTrue(any("登录成功" in message for message in messages))

    async def test_legacy_pending_keys_still_support_successful_login(self):
        for legacy_key in self.keys[1:]:
            with self.subTest(legacy_key=legacy_key):
                await self.pending(key=legacy_key)
                with patch.object(main.nte, "build_account_by_sms", return_value=copy.deepcopy(ACCOUNT)) as login:
                    messages = await self.handle("123456")
                login.assert_called_once_with(PHONE, "123456", "device")
                self.assertTrue(any("登录成功" in message for message in messages))
                self.assertIn(self.keys[0], self.plugin.storage["users"])
                self.assertFalse(self.pending_keys())

    async def test_cancel_clears_all_compatible_keys_and_preserves_bound_users(self):
        session = await self.pending()
        self.plugin.storage["pending_login"].update({key: copy.deepcopy(session) for key in self.keys})
        self.plugin.storage["pending_login"]["test:99"] = copy.deepcopy(session)
        self.plugin.storage["users"] = {self.keys[0]: {"account": copy.deepcopy(ACCOUNT), "phone": PHONE}}
        users_before = copy.deepcopy(self.plugin.storage["users"])
        messages = await collect(self.plugin.ntecancel(MessageEvent("/ntecancel")))
        self.assertTrue(messages)
        self.assertEqual({"test:99"}, self.pending_keys())
        self.assertEqual(users_before, self.plugin.storage["users"])
        self.assertEqual([], await self.handle("123456"))

    async def test_cancel_in_group_does_not_touch_private_session(self):
        await self.pending()
        before = copy.deepcopy(self.plugin.storage)
        messages = await collect(self.plugin.ntecancel(MessageEvent("/ntecancel", group="group")))
        self.assertTrue(any("私聊" in message for message in messages))
        self.assertEqual(before, self.plugin.storage)

    async def test_no_session_and_other_users_messages_are_silent(self):
        with patch.object(main.nte, "build_account_by_sms") as login:
            self.assertEqual([], await self.handle("123456"))
            await self.pending()
            self.assertEqual([], await self.handle("123456", sender="99"))
        login.assert_not_called()
        self.assertEqual({self.keys[0]}, self.pending_keys())

    async def test_resending_sms_clears_old_aliases_even_when_sending_fails(self):
        for mode, (command, api) in REQUEST_COMMAND.items():
            with self.subTest(mode=mode):
                self.plugin.storage.pop("captcha_limits", None)
                session = await self.pending()
                self.plugin.storage["pending_login"].update({key: copy.deepcopy(session) for key in self.keys})
                self.plugin.storage["pending_login"]["test:99"] = copy.deepcopy(session)

                def send(*args):
                    self.assertEqual({"test:99"}, self.pending_keys())
                    raise RuntimeError("发送失败")

                with patch.object(main.nte, api, side_effect=send) as send_api:
                    messages = await collect(getattr(self.plugin, command)(self.event, OTHER_PHONE))
                send_api.assert_called_once_with(OTHER_PHONE)
                self.assertTrue(any("失败" in message for message in messages))
                self.assertEqual({"test:99"}, self.pending_keys())

    async def test_resending_sms_installs_only_the_new_session(self):
        for mode, (command, api) in REQUEST_COMMAND.items():
            with self.subTest(mode=mode):
                self.plugin.storage.pop("captcha_limits", None)
                session = await self.pending()
                self.plugin.storage["pending_login"].update({key: copy.deepcopy(session) for key in self.keys})
                with patch.object(main.nte, api, return_value="new-device"):
                    await collect(getattr(self.plugin, command)(self.event, OTHER_PHONE))
                self.assertEqual({self.keys[0]}, self.pending_keys())
                pending = self.plugin.storage["pending_login"][self.keys[0]]
                self.assertEqual(mode, pending["mode"])
                self.assertEqual(OTHER_PHONE, pending["phone"])
                self.assertEqual("new-device", pending["device_id"])

    async def test_sms_cooldown_applies_across_modes_users_and_phones(self):
        for event, phone in (
            (self.event, PHONE),
            (self.event, OTHER_PHONE),
            (MessageEvent(sender="99"), PHONE),
        ):
            with self.subTest(sender=event.sender, phone=phone):
                self.plugin = main.NTEPlugin(object(), {})
                with patch.object(main.nte, "send_login_captcha", return_value="device") as send:
                    await collect(self.plugin.nteph(self.event, PHONE))
                    before = copy.deepcopy(self.plugin.storage["pending_login"])
                    with patch.object(main.nte, "send_cloud_login_captcha") as send_cloud:
                        messages = await collect(self.plugin.nteyun(event, phone))
                send.assert_called_once()
                send_cloud.assert_not_called()
                self.assertTrue(messages)
                self.assertEqual(before, self.plugin.storage["pending_login"])

    async def test_sms_daily_limit_resets_on_next_day(self):
        FrozenDateTime.current = datetime(2026, 10, 5, 10, 0, 0)
        with patch.object(main, "datetime", FrozenDateTime):
            with patch.object(main.nte, "send_login_captcha", return_value="device") as send:
                for _ in range(10):
                    messages = await collect(self.plugin.nteph(self.event, PHONE))
                    self.assertTrue(any("已发送" in message for message in messages))
                    FrozenDateTime.current += timedelta(seconds=61)
                before = copy.deepcopy(self.plugin.storage["pending_login"])
                messages = await collect(self.plugin.nteph(self.event, PHONE))
                self.assertEqual(10, send.call_count)
                self.assertTrue(messages)
                self.assertFalse(any("已发送" in message for message in messages))
                self.assertEqual(before, self.plugin.storage["pending_login"])
                FrozenDateTime.current += timedelta(days=1)
                messages = await collect(self.plugin.nteph(self.event, PHONE))
                self.assertEqual(11, send.call_count)
                self.assertTrue(any("已发送" in message for message in messages))

    async def test_failed_sms_request_still_reserves_cooldown(self):
        with patch.object(main.nte, "send_login_captcha", side_effect=RuntimeError("发送失败")) as send:
            first = await collect(self.plugin.nteph(self.event, PHONE))
            second = await collect(self.plugin.nteph(self.event, PHONE))
        self.assertTrue(any("失败" in message for message in first))
        self.assertTrue(second)
        send.assert_called_once()
        self.assertFalse(self.pending_keys())

    async def test_two_simultaneous_sms_commands_make_only_one_send_request(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def to_thread(function, *args):
            calls.append(args)
            started.set()
            await release.wait()
            return "new-device"

        with patch.object(main.asyncio, "to_thread", side_effect=to_thread):
            first = asyncio.create_task(collect(self.plugin.nteph(self.event, PHONE)))
            await asyncio.wait_for(started.wait(), 2)
            second = await asyncio.wait_for(collect(self.plugin.nteyun(self.event, PHONE)), 2)
            release.set()
            await asyncio.wait_for(first, 2)
        self.assertTrue(second)
        self.assertEqual(1, len(calls))
        self.assertEqual("sms", self.plugin.storage["pending_login"][self.keys[0]]["mode"])

    async def test_cancel_during_sms_request_prevents_late_session_creation(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def to_thread(function, *args):
            started.set()
            await release.wait()
            return "new-device"

        with patch.object(main.asyncio, "to_thread", side_effect=to_thread):
            request = asyncio.create_task(collect(self.plugin.nteph(self.event, PHONE)))
            await asyncio.wait_for(started.wait(), 2)
            await collect(self.plugin.ntecancel(MessageEvent("/ntecancel")))
            release.set()
            await asyncio.wait_for(request, 2)
        self.assertFalse(self.pending_keys())

    async def test_success_merges_same_phone_and_migrates_legacy_user(self):
        self.plugin.storage["users"] = {
            self.keys[1]: {"account": copy.deepcopy(ACCOUNT), "phone": PHONE, "bound_at": "2026-01-01T00:00:00"},
            "test:99": {"account": copy.deepcopy(ACCOUNT), "phone": OTHER_PHONE},
        }
        other_before = copy.deepcopy(self.plugin.storage["users"]["test:99"])
        await self.pending("cloud_sms")
        with patch.object(main.nte, "build_cloud_account_by_sms", return_value=copy.deepcopy(CLOUD_ACCOUNT)):
            messages = await self.handle("123456")
        self.assertTrue(any("已更新" in message for message in messages))
        users = self.plugin.storage["users"]
        self.assertNotIn(self.keys[1], users)
        self.assertEqual(other_before, users["test:99"])
        accounts = users[self.keys[0]]["accounts"]
        self.assertEqual(1, len(accounts))
        self.assertEqual("both", accounts[0]["kind"])
        self.assertEqual("refresh", accounts[0]["account"]["refreshToken"])
        self.assertEqual("cloud-token", accounts[0]["account"]["cloudToken"])
        self.assertFalse(self.pending_keys())

    async def test_two_simultaneous_codes_make_only_one_login_attempt(self):
        await self.pending()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def to_thread(function, *args):
            calls.append(args)
            started.set()
            await release.wait()
            return copy.deepcopy(ACCOUNT)

        with patch.object(main.asyncio, "to_thread", side_effect=to_thread):
            first = asyncio.create_task(self.handle("123456"))
            await asyncio.wait_for(started.wait(), 2)
            second = asyncio.create_task(self.handle("123456"))
            await asyncio.sleep(0)
            release.set()
            messages = await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, sum("登录成功" in text for group in messages for text in group))

    async def test_refreshing_session_while_old_attempt_runs_preserves_new_session(self):
        for old_success in (False, True):
            with self.subTest(old_success=old_success):
                await self.pending()
                started = asyncio.Event()
                release = asyncio.Event()

                async def to_thread(function, *args):
                    started.set()
                    await release.wait()
                    if old_success:
                        return copy.deepcopy(ACCOUNT)
                    raise RuntimeError("验证码已失效")

                with patch.object(main.asyncio, "to_thread", side_effect=to_thread):
                    old_attempt = asyncio.create_task(self.handle("123456"))
                    await asyncio.wait_for(started.wait(), 2)
                    await self.pending(phone=OTHER_PHONE)
                    new_session = copy.deepcopy(self.plugin.storage["pending_login"])
                    release.set()
                    await asyncio.wait_for(old_attempt, 2)
                self.assertEqual(new_session, self.plugin.storage["pending_login"])

    async def test_cancel_during_login_prevents_late_binding(self):
        await self.pending()
        self.plugin.storage["users"] = {"test:99": {"account": copy.deepcopy(ACCOUNT), "phone": OTHER_PHONE}}
        users_before = copy.deepcopy(self.plugin.storage["users"])
        started = asyncio.Event()
        release = asyncio.Event()

        async def to_thread(function, *args):
            started.set()
            await release.wait()
            return copy.deepcopy(ACCOUNT)

        with patch.object(main.asyncio, "to_thread", side_effect=to_thread):
            attempt = asyncio.create_task(self.handle("123456"))
            await asyncio.wait_for(started.wait(), 2)
            await collect(self.plugin.ntecancel(MessageEvent("/ntecancel")))
            release.set()
            messages = await asyncio.wait_for(attempt, 2)
        self.assertFalse(any("登录成功" in message for message in messages))
        self.assertEqual(users_before, self.plugin.storage["users"])
        self.assertFalse(self.pending_keys())


if __name__ == "__main__":
    unittest.main()
