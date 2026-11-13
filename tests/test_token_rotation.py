import asyncio
import copy
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from .support import MessageEvent, collect, load_nte_module, load_plugin_module
except ImportError:
    from support import MessageEvent, collect, load_nte_module, load_plugin_module


main = load_plugin_module()
nte = load_nte_module()


class TokenRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = SimpleNamespace(send_message=AsyncMock())
        self.plugin = main.NTEPlugin(
            self.context, {"auto_sign_enabled": True, "auto_sign_delay": 0}
        )
        self.event = MessageEvent(sender="alice")
        self.key = self.plugin._build_user_keys(self.event)[0]
        self.expected_token = "fake-initial"
        self.used_tokens = []
        self.fail_community = False
        self.entry = {
            "account": {
                "uid": "1", "refreshToken": self.expected_token,
                "deviceId": "device1", "gameId": "1289", "roleIds": ["role1"],
            },
            "phone": "13800138000", "bound_at": "2026-11-13T00:00:00",
        }
        user = {"umo": self.event.unified_msg_origin}
        self.plugin._store_accounts(user, [self.entry])
        await self.plugin.put_kv_data("users", {self.key: user})

    def saved_token(self):
        return self.plugin.storage["users"][self.key]["accounts"][0]["account"]["refreshToken"]

    def response(self, payload, status=200):
        response = nte.requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        return response

    def respond(self, url, **kwargs):
        headers = kwargs["headers"]
        if url == nte.REFRESH_TOKEN_URL:
            self.assertEqual(headers["appversion"], "1.2.5")
            self.assertEqual(headers["uid"], "1")
            self.assertEqual(headers["debug-uid"], "3")
            self.assertRegex(headers["ds"], r"^\d+,[A-Za-z0-9]{8},[0-9a-f]{32}$")
            token = headers["authorization"]
            self.used_tokens.append(token)
            if token != self.expected_token:
                return self.response({}, status=402)
            self.expected_token += "-next"
            return self.response({"code": 0, "data": {
                "accessToken": "fake-access", "refreshToken": self.expected_token,
            }})
        if url == nte.APP_SIGNIN_URL:
            self.assertEqual(self.saved_token(), self.expected_token)
            if self.fail_community:
                raise nte.requests.Timeout()
        return self.response({"code": 0, "data": {}})

    async def run_real_sign(self, automatic=False):
        with patch.object(main, "nte", nte), \
                patch.object(nte.requests, "post", side_effect=self.respond), \
                patch.object(nte.requests, "get", side_effect=self.respond):
            if automatic:
                await self.plugin._auto_sign_all_users()
                return ""
            return "\n".join(await collect(self.plugin.nte_sign(self.event)))

    async def test_two_successful_runs_and_plugin_restart_use_latest_token(self):
        await self.run_real_sign()
        await self.run_real_sign()
        stored = copy.deepcopy(self.plugin.storage)
        self.plugin = main.NTEPlugin(self.context, {})
        self.plugin.storage = stored
        await self.run_real_sign()
        self.assertEqual(self.used_tokens, [
            "fake-initial", "fake-initial-next", "fake-initial-next-next",
        ])
        self.assertEqual(self.saved_token(), "fake-initial-next-next-next")

    async def assert_failure_retains_token(self, automatic):
        self.fail_community = True
        await self.run_real_sign(automatic)
        self.assertEqual(self.saved_token(), "fake-initial-next")
        self.fail_community = False
        await self.run_real_sign(automatic)
        self.assertEqual(self.used_tokens, ["fake-initial", "fake-initial-next"])

    async def test_manual_failure_after_refresh_preserves_new_token(self):
        await self.assert_failure_retains_token(False)

    async def test_automatic_failure_after_refresh_preserves_new_token(self):
        await self.assert_failure_retains_token(True)

    async def test_failed_checkpoint_prevents_community_request(self):
        original_write = self.plugin.put_kv_data

        async def fail_rotated_write(key, value):
            token = value[self.key]["accounts"][0]["account"]["refreshToken"]
            if token != "fake-initial":
                raise OSError("simulated storage failure")
            await original_write(key, value)

        with patch.object(self.plugin, "put_kv_data", side_effect=fail_rotated_write), \
                patch.object(nte, "app_signin") as community:
            await self.run_real_sign()
        community.assert_not_called()
        self.assertEqual(self.saved_token(), "fake-initial")

    async def test_cancelled_sign_keeps_guard_until_worker_finishes(self):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        calls = []

        def service(account, *, output, persist):
            calls.append(account["refreshToken"])
            account["refreshToken"] = "fake-after-cancel"
            persist()
            loop.call_soon_threadsafe(started.set)
            self.assertTrue(release.wait(5))
            return True

        with patch.object(main.nte, "do_sign", side_effect=service):
            task = asyncio.create_task(collect(self.plugin.nte_sign(self.event)))
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(self.saved_token(), "fake-after-cancel")
                await collect(self.plugin.nte_sign(self.event))
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
        self.assertFalse(self.plugin._signing_accounts)

    async def test_logout_before_checkpoint_does_not_restore_account(self):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def service(account, *, output, persist):
            account["refreshToken"] = "fake-late-refresh"
            loop.call_soon_threadsafe(started.set)
            self.assertTrue(release.wait(5))
            persist()
            self.fail("Signing must stop after the binding was removed")

        with patch.object(main.nte, "do_sign", side_effect=service):
            task = asyncio.create_task(collect(self.plugin.nte_sign(self.event)))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await collect(self.plugin.ntelogout(self.event))
            finally:
                release.set()
                messages = await asyncio.wait_for(task, 2)
        self.assertNotIn(self.key, self.plugin.storage["users"])
        self.assertIn("账号绑定已变更", "\n".join(messages))

    async def test_relogin_after_checkpoint_preserves_new_binding(self):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def service(account, *, output, persist):
            account["refreshToken"] = "fake-checkpoint"
            persist()
            loop.call_soon_threadsafe(started.set)
            self.assertTrue(release.wait(5))
            return True

        with patch.object(main.nte, "do_sign", side_effect=service):
            task = asyncio.create_task(collect(self.plugin.nte_sign(self.event)))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await collect(self.plugin.ntepw(self.event, self.entry["phone"]))
                new_account = dict(self.entry["account"], refreshToken="fake-relogin")
                with patch.object(main.nte, "build_account_by_password", return_value=new_account):
                    await collect(self.plugin.handle_pending_login_input(
                        MessageEvent("fake-password", sender="alice")
                    ))
                self.assertEqual(self.saved_token(), "fake-relogin")
            finally:
                release.set()
                await asyncio.wait_for(task, 2)
        self.assertEqual(self.saved_token(), "fake-relogin")


if __name__ == "__main__":
    unittest.main()
