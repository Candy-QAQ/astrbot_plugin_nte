"""Exercise real handlers while account work is paused outside the KV lock."""

import asyncio
import copy
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import AsyncMock, patch

try:
    from .support import MessageEvent, collect, load_plugin_module
except ImportError:
    from support import MessageEvent, collect, load_plugin_module


main = load_plugin_module()
PHONE = "13800138000"
OTHER_PHONE = "13900139000"


class UserConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = SimpleNamespace(send_message=AsyncMock())
        self.plugin = main.NTEPlugin(
            self.context,
            {"auto_sign_enabled": True, "auto_sign_delay": 0, "max_users": 0},
        )
        self.alice = MessageEvent(sender="alice")
        self.bob = MessageEvent(sender="bob")

    def user_key(self, event):
        return self.plugin._build_user_keys(event)[0]

    def account_entry(self, phone=PHONE, token="old-token"):
        return {
            "account": {"uid": phone, "refreshToken": token, "gameId": "1289"},
            "phone": phone,
            "kind": "tajiduo",
            "bound_at": "2026-09-01T00:00:00",
            "last_sign_at": None,
        }

    async def bind_initial(self, event, entries=None):
        users = await self.plugin.get_kv_data("users", {})
        user_data = {
            "umo": event.unified_msg_origin,
            "platform_name": event.get_platform_name(),
        }
        self.plugin._store_accounts(user_data, entries or [self.account_entry()])
        users[self.user_key(event)] = user_data
        await self.plugin.put_kv_data("users", users)

    async def users(self):
        return await self.plugin.get_kv_data("users", {})

    async def begin_sign(self, kind, event):
        if kind == "auto":
            return asyncio.create_task(self.plugin._auto_sign_all_users())
        return asyncio.create_task(collect(self.plugin.nte_sign(event)))

    async def assert_logout_during_sign(self, kind, *, index=""):
        entries = [self.account_entry()]
        if index:
            entries.append(self.account_entry(OTHER_PHONE, "other-token"))
        await self.bind_initial(self.alice, entries)
        started, release = asyncio.Event(), asyncio.Event()

        async def paused_sign(entry):
            started.set()
            await release.wait()
            entry["account"]["refreshToken"] = "old-sign-result"
            entry["last_sign_at"] = "2026-10-05T09:00:00"
            return True, []

        with patch.object(self.plugin, "_do_sign_for_account", side_effect=paused_sign):
            task = await self.begin_sign(kind, self.alice)
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(collect(self.plugin.ntelogout(self.alice, index)), 2)
                after_logout = await self.users()
            finally:
                release.set()
                await asyncio.wait_for(task, 2)

        users = await self.users()
        if not index:
            self.assertNotIn(self.user_key(self.alice), after_logout)
            self.assertNotIn(self.user_key(self.alice), users)
        else:
            accounts = users[self.user_key(self.alice)]["accounts"]
            self.assertEqual([OTHER_PHONE], [entry["phone"] for entry in accounts])
            self.assertNotIn(PHONE, [entry["phone"] for entry in accounts])

    async def test_auto_sign_cannot_restore_logged_out_user(self):
        await self.assert_logout_during_sign("auto")

    async def test_manual_sign_cannot_restore_logged_out_user(self):
        await self.assert_logout_during_sign("manual")

    async def test_manual_sign_cannot_restore_removed_account_or_use_stale_index(self):
        await self.assert_logout_during_sign("manual", index="1")

    async def test_auto_sign_cannot_restore_removed_account_or_use_stale_index(self):
        await self.assert_logout_during_sign("auto", index="1")

    async def assert_relogin_during_sign(self, kind):
        await self.bind_initial(self.alice)
        started, release = asyncio.Event(), asyncio.Event()

        async def paused_sign(entry):
            started.set()
            await release.wait()
            entry["account"]["refreshToken"] = "stale-refreshed-token"
            entry["last_sign_at"] = "2026-10-05T09:00:00"
            return True, []

        new_account = {
            "uid": PHONE,
            "refreshToken": "fresh-login-token",
            "gameId": "1289",
        }
        with patch.object(self.plugin, "_do_sign_for_account", side_effect=paused_sign):
            task = await self.begin_sign(kind, self.alice)
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(collect(self.plugin.ntepw(self.alice, PHONE)), 2)
                with patch.object(main.nte, "build_account_by_password", return_value=new_account):
                    result = await asyncio.wait_for(
                        collect(self.plugin.handle_pending_login_input(
                            MessageEvent("new-password", sender="alice")
                        )),
                        2,
                    )
                self.assertIn("登录成功", "\n".join(result))
                after_login = await self.users()
                fresh_entry = copy.deepcopy(after_login[self.user_key(self.alice)]["accounts"][0])
            finally:
                release.set()
                await asyncio.wait_for(task, 2)

        users = await self.users()
        accounts = users[self.user_key(self.alice)]["accounts"]
        self.assertEqual(1, len(accounts))
        self.assertEqual("fresh-login-token", accounts[0]["account"]["refreshToken"])
        self.assertEqual(fresh_entry, accounts[0])

    async def test_manual_sign_does_not_overwrite_new_login_credentials(self):
        await self.assert_relogin_during_sign("manual")

    async def test_auto_sign_does_not_overwrite_new_login_credentials(self):
        await self.assert_relogin_during_sign("auto")

    async def test_parallel_signs_for_two_users_preserve_both_updates(self):
        await self.bind_initial(self.alice, [self.account_entry(PHONE, "alice-old")])
        await self.bind_initial(self.bob, [self.account_entry(OTHER_PHONE, "bob-old")])
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def paused_sign(entry):
            calls.append(entry["phone"])
            if len(calls) == 2:
                started.set()
            await release.wait()
            entry["account"]["refreshToken"] = f"fresh-{entry['phone']}"
            entry["last_sign_at"] = "2026-10-05T09:00:00"
            return True, []

        with patch.object(self.plugin, "_do_sign_for_account", side_effect=paused_sign):
            tasks = [
                asyncio.create_task(collect(self.plugin.nte_sign(event)))
                for event in (self.alice, self.bob)
            ]
            try:
                await asyncio.wait_for(started.wait(), 2)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 2)

        users = await self.users()
        self.assertEqual({self.user_key(self.alice), self.user_key(self.bob)}, set(users))
        for event, phone in ((self.alice, PHONE), (self.bob, OTHER_PHONE)):
            account = users[self.user_key(event)]["accounts"][0]["account"]
            self.assertEqual(f"fresh-{phone}", account["refreshToken"])

    async def test_sign_for_one_user_does_not_restore_another_logged_out_user(self):
        await self.bind_initial(self.alice)
        await self.bind_initial(self.bob, [self.account_entry(OTHER_PHONE, "bob-old")])
        started, release = asyncio.Event(), asyncio.Event()

        async def paused_sign(entry):
            started.set()
            await release.wait()
            entry["account"]["refreshToken"] = "alice-fresh"
            return True, []

        with patch.object(self.plugin, "_do_sign_for_account", side_effect=paused_sign):
            task = asyncio.create_task(collect(self.plugin.nte_sign(self.alice)))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(collect(self.plugin.ntelogout(self.bob)), 2)
            finally:
                release.set()
                await asyncio.wait_for(task, 2)

        users = await self.users()
        self.assertEqual({self.user_key(self.alice)}, set(users))
        self.assertEqual("alice-fresh", users[self.user_key(self.alice)]["accounts"][0]["account"]["refreshToken"])

    async def test_auto_sign_skips_other_user_deleted_after_snapshot(self):
        await self.bind_initial(self.alice)
        await self.bind_initial(self.bob, [self.account_entry(OTHER_PHONE, "bob-old")])
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def paused_sign(entry):
            calls.append(entry["phone"])
            started.set()
            await release.wait()
            entry["account"]["refreshToken"] = "alice-fresh"
            return True, []

        with patch.object(self.plugin, "_do_sign_for_account", side_effect=paused_sign):
            task = asyncio.create_task(self.plugin._auto_sign_all_users())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(collect(self.plugin.ntelogout(self.bob)), 2)
            finally:
                release.set()
                await asyncio.wait_for(task, 2)

        self.assertEqual([PHONE], calls)
        self.assertEqual({self.user_key(self.alice)}, set(await self.users()))
        self.assertEqual(1, self.context.send_message.await_count)
        self.assertEqual(self.alice.unified_msg_origin, self.context.send_message.await_args.args[0])

    async def test_legacy_key_migration_does_not_restore_another_logged_out_user(self):
        await self.bind_initial(self.alice)
        await self.bind_initial(self.bob, [self.account_entry(OTHER_PHONE, "bob-old")])
        users = await self.users()
        users[self.alice.sender] = users.pop(self.user_key(self.alice))
        await self.plugin.put_kv_data("users", users)

        await asyncio.gather(
            collect(self.plugin.ntelogout(self.bob)),
            collect(self.plugin.ntelist(self.alice)),
        )

        self.assertEqual({self.user_key(self.alice)}, set(await self.users()))

    async def test_migrated_legacy_alias_does_not_bind_another_platform_user(self):
        await self.bind_initial(self.alice)
        users = await self.users()
        users[self.alice.sender] = users.pop(self.user_key(self.alice))
        await self.plugin.put_kv_data("users", users)
        await collect(self.plugin.ntelist(self.alice))
        before = await self.users()

        other_platform = MessageEvent(sender="alice", platform="other")
        messages = await collect(self.plugin.ntelist(other_platform))

        self.assertIn("还未绑定账号", "\n".join(messages))
        self.assertEqual(before, await self.users())

    async def test_parallel_logins_for_two_users_preserve_both_bindings(self):
        await asyncio.gather(
            collect(self.plugin.ntepw(self.alice, PHONE)),
            collect(self.plugin.ntepw(self.bob, OTHER_PHONE)),
        )

        def login(phone, password):
            return {"uid": phone, "refreshToken": f"login-{phone}", "gameId": "1289"}

        with patch.object(main.nte, "build_account_by_password", side_effect=login):
            results = await asyncio.gather(*(
                collect(self.plugin.handle_pending_login_input(
                    MessageEvent("password", sender=event.sender)
                ))
                for event in (self.alice, self.bob)
            ))

        for messages in results:
            self.assertIn("登录成功", "\n".join(messages))
        users = await self.users()
        self.assertEqual({self.user_key(self.alice), self.user_key(self.bob)}, set(users))
        for event, phone in ((self.alice, PHONE), (self.bob, OTHER_PHONE)):
            self.assertEqual(f"login-{phone}", users[self.user_key(event)]["accounts"][0]["account"]["refreshToken"])

    async def assert_manual_and_auto_share_sign_guard(self, first_kind, *, legacy_key=False):
        await self.bind_initial(self.alice)
        if legacy_key:
            users = await self.users()
            users[self.alice.sender] = users.pop(self.user_key(self.alice))
            await self.plugin.put_kv_data("users", users)
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        calls = []

        def account_service(account, *, output):
            calls.append(account["refreshToken"])
            if len(calls) == 1:
                loop.call_soon_threadsafe(started.set)
                self.assertTrue(release.wait(5), "The paused account service was not released")
            account["refreshToken"] = "fresh-sign-token"
            output("账号测试：社区签到成功")
            return True

        with patch.object(main.nte, "do_sign", side_effect=account_service):
            first = await self.begin_sign(first_kind, self.alice)
            try:
                await asyncio.wait_for(started.wait(), 2)
                if legacy_key:
                    # A read command migrates the user while automatic signing is in flight.
                    await collect(self.plugin.ntelist(self.alice))
                second_kind = "manual" if first_kind == "auto" else "auto"
                second = await self.begin_sign(second_kind, self.alice)
                await asyncio.wait_for(second, 2)
                self.assertEqual(1, len(calls))
            finally:
                release.set()
                await asyncio.wait_for(first, 2)

        self.assertFalse(self.plugin._signing_accounts)
        users = await self.users()
        self.assertEqual({self.user_key(self.alice)}, set(users))
        self.assertEqual(
            "fresh-sign-token",
            users[self.user_key(self.alice)]["accounts"][0]["account"]["refreshToken"],
        )

    async def test_auto_during_manual_sign_calls_account_service_only_once(self):
        await self.assert_manual_and_auto_share_sign_guard("manual")

    async def test_manual_during_auto_sign_calls_account_service_only_once(self):
        await self.assert_manual_and_auto_share_sign_guard("auto")

    async def test_legacy_key_migration_keeps_same_account_sign_guard(self):
        await self.assert_manual_and_auto_share_sign_guard("auto", legacy_key=True)

    async def test_login_completion_rechecks_max_users_after_another_user_binds(self):
        self.plugin.config["max_users"] = 1
        await asyncio.gather(
            collect(self.plugin.ntepw(self.alice, PHONE)),
            collect(self.plugin.ntepw(self.bob, OTHER_PHONE)),
        )
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()

        def login(phone, password):
            if phone == PHONE:
                loop.call_soon_threadsafe(started.set)
                self.assertTrue(release.wait(5), "The paused login service was not released")
            return {"uid": phone, "refreshToken": f"login-{phone}", "gameId": "1289"}

        with patch.object(main.nte, "build_account_by_password", side_effect=login):
            alice_task = asyncio.create_task(collect(self.plugin.handle_pending_login_input(
                MessageEvent("password", sender="alice")
            )))
            try:
                await asyncio.wait_for(started.wait(), 2)
                bob_result = await collect(self.plugin.handle_pending_login_input(
                    MessageEvent("password", sender="bob")
                ))
                self.assertIn("登录成功", "\n".join(bob_result))
            finally:
                release.set()
                alice_result = await asyncio.wait_for(alice_task, 2)

        self.assertIn("最大用户数限制", "\n".join(alice_result))
        self.assertEqual({self.user_key(self.bob)}, set(await self.users()))
        self.assertFalse(self.plugin._login_requests)
        self.assertFalse(self.plugin.storage.get("pending_login", {}))

    def test_game_id_metadata_is_removed_for_half_and_full_width_parentheses(self):
        for opening, closing in (("(", ")"), ("（", "）")):
            with self.subTest(parentheses=(opening, closing)):
                parsed = self.plugin._parse_detail_lines([
                    f"角色BBB(222)签到失败：签到失败{opening}gameId=1257{closing}"
                ])
                self.assertEqual(["游戏  ❌ BBB — 签到失败"], parsed["games"])


if __name__ == "__main__":
    unittest.main()
