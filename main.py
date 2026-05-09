"""
AstrBot Plugin - 异环签到 (NTE Auto Sign)

Commands:
- ntepw <手机号> (private): 输入手机号后，下一条私聊消息输入密码完成登录
- nteph <手机号> (private): 获取验证码后，下一条私聊消息输入验证码完成登录
- nte (private): 立即签到
- ntelist (private): 查看当前已绑定账号
- ntelogout (private): 解除绑定
- ntehelp: 查看帮助
"""

from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.config import put_config
import asyncio
import contextlib
import copy
import io
import random
import re

from . import nte

PLUGIN_NAME = "astrbot_plugin_nte"
PENDING_EXPIRE_SECONDS = 600
PHONE_RE = re.compile(r"^1\d{10}$")
COMMAND_TEXT_RE = re.compile(r"^/?(nte|ntepw|nteph|ntelist|ntelogout|ntehelp)(\s|$)", re.IGNORECASE)


@register(PLUGIN_NAME, "AstrBot", "异环自动签到插件", "1.0.0")
class NTEPlugin(Star):
    """异环签到插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self._init_config()

    def _init_config(self):
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到开关",
            key="auto_sign_enabled",
            value=False,
            description="开启后，将在指定时间自动为已绑定用户签到，并私发结果",
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到时间（小时）",
            key="auto_sign_hour",
            value=9,
            description="自动签到执行的小时（0-23）",
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到时间（分钟）",
            key="auto_sign_minute",
            value=0,
            description="自动签到执行的分钟（0-59）",
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="自动签到随机延迟",
            key="auto_sign_delay",
            value=10,
            description="每个用户签到前随机延迟秒数上限（0为不延迟）",
        )
        put_config(
            namespace=PLUGIN_NAME,
            name="最大绑定用户数",
            key="max_users",
            value=20,
            description="0 为不限制，超过限制则不允许新用户绑定",
        )

    def _get_config(self) -> dict:
        return {
            "auto_sign_enabled": self.config.get("auto_sign_enabled", False),
            "auto_sign_hour": self.config.get("auto_sign_hour", 9),
            "auto_sign_minute": self.config.get("auto_sign_minute", 0),
            "auto_sign_delay": self.config.get("auto_sign_delay", 10),
            "max_users": self.config.get("max_users", 20),
        }

    async def initialize(self):
        logger.info("异环签到插件已加载")
        config = self._get_config()
        if config.get("auto_sign_enabled", False):
            self._start_auto_sign_job(
                config.get("auto_sign_hour", 9),
                config.get("auto_sign_minute", 0),
            )
        if not self.scheduler.running:
            self.scheduler.start()

    async def terminate(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("异环签到插件已卸载")

    def _start_auto_sign_job(self, hour: int, minute: int):
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        trigger = CronTrigger(hour=hour, minute=minute)
        try:
            self.scheduler.remove_job("nte_auto_sign")
        except Exception:
            pass
        self.scheduler.add_job(
            self._auto_sign_all_users,
            trigger=trigger,
            id="nte_auto_sign",
            misfire_grace_time=3600,
        )
        logger.info(f"异环自动签到任务已启动，每天 {hour:02d}:{minute:02d} 执行")

    async def _send_private_message(self, user_id: str, user_data: dict, message: str):
        try:
            umo = user_data.get("umo")
            if not umo:
                logger.warning(f"用户 {user_id} 没有统一会话ID，无法发送私聊消息")
                return
            await self.context.send_message(umo, MessageChain().message(message))
        except Exception as e:
            logger.error(f"发送私聊消息失败: {e}")

    def _is_private(self, event: AstrMessageEvent) -> bool:
        return not bool(getattr(event.message_obj, "group_id", None))

    def _valid_phone(self, phone: str) -> bool:
        return bool(PHONE_RE.fullmatch((phone or "").strip()))

    def _build_user_keys(self, event: AstrMessageEvent) -> list[str]:
        keys: list[str] = []
        platform_name = str(event.get_platform_name() or "").strip().lower()
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id:
            if platform_name:
                keys.append(f"{platform_name}:{sender_id}")
            keys.append(sender_id)  # 兼容旧版本仅使用 sender_id 的存储键
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            keys.append(f"umo:{umo}")
        deduped: list[str] = []
        for key in keys:
            if key and key not in deduped:
                deduped.append(key)
        return deduped

    @staticmethod
    def _pick_existing_key(store: dict, keys: list[str]) -> str | None:
        for key in keys:
            if key in store:
                return key
        return None

    def _normalize_accounts(self, user_data: dict) -> list[dict]:
        accounts = user_data.get("accounts")
        normalized: list[dict] = []
        if isinstance(accounts, list):
            for item in accounts:
                if not isinstance(item, dict):
                    continue
                account = copy.deepcopy(item.get("account") or {})
                if not account or not account.get("refreshToken"):
                    continue
                normalized.append(
                    {
                        "account": account,
                        "phone": str(item.get("phone", "")).strip(),
                        "bound_at": item.get("bound_at") or datetime.now().isoformat(),
                        "last_sign_at": item.get("last_sign_at"),
                    }
                )
        if normalized:
            return normalized

        legacy_account = copy.deepcopy(user_data.get("account") or {})
        if legacy_account and legacy_account.get("refreshToken"):
            return [
                {
                    "account": legacy_account,
                    "phone": str(user_data.get("phone", "")).strip(),
                    "bound_at": user_data.get("bound_at") or datetime.now().isoformat(),
                    "last_sign_at": user_data.get("last_sign_at"),
                }
            ]
        return []

    def _store_accounts(self, user_data: dict, accounts: list[dict]):
        user_data["accounts"] = accounts
        if accounts:
            latest = accounts[-1]
            user_data["account"] = copy.deepcopy(latest.get("account") or {})
            user_data["phone"] = latest.get("phone", "")
            user_data["bound_at"] = latest.get("bound_at")
            user_data["last_sign_at"] = latest.get("last_sign_at")
        else:
            user_data.pop("account", None)
            user_data.pop("phone", None)
            user_data.pop("bound_at", None)
            user_data.pop("last_sign_at", None)

    def _upsert_account(self, accounts: list[dict], new_entry: dict) -> tuple[str, int]:
        new_account = new_entry.get("account") or {}
        new_uid = str(new_account.get("uid") or "").strip()
        new_game_id = str(new_account.get("gameId") or "").strip()
        new_phone = str(new_entry.get("phone") or "").strip()

        for idx, item in enumerate(accounts):
            account = item.get("account") or {}
            uid = str(account.get("uid") or "").strip()
            game_id = str(account.get("gameId") or "").strip()
            phone = str(item.get("phone") or "").strip()
            if new_uid and uid == new_uid and new_game_id and game_id == new_game_id:
                accounts[idx] = new_entry
                return "updated", idx
            if new_phone and phone == new_phone and uid == new_uid:
                accounts[idx] = new_entry
                return "updated", idx

        accounts.append(new_entry)
        return "added", len(accounts) - 1

    def _format_account_brief(self, entry: dict, index: int) -> str:
        account = entry.get("account") or {}
        uid = str(account.get("uid") or "?")
        game_id = str(account.get("gameId") or "?")
        role_count = len(account.get("roleIds") or [])
        phone = str(entry.get("phone") or "").strip()
        phone_tail = phone[-4:] if len(phone) >= 4 else phone
        parts = [f"{index}. uid={uid}", f"gameId={game_id}", f"角色={role_count}"]
        if phone_tail:
            parts.append(f"手机号尾号={phone_tail}")
        return " | ".join(parts)

    async def _do_sign_for_account(self, entry: dict) -> tuple[bool, list[str]]:
        account = copy.deepcopy(entry.get("account") or {})
        if not account or not account.get("refreshToken"):
            raise Exception("账号数据缺失，请重新登录")

        def _run_sign():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                sign_ok = nte.do_sign(account)
            text = buf.getvalue()
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return sign_ok, lines

        ok, details = await asyncio.to_thread(_run_sign)
        entry["account"] = account
        entry["last_sign_at"] = datetime.now().isoformat()
        return ok, details

    async def _auto_sign_all_users(self):
        config = self._get_config()
        if not config.get("auto_sign_enabled", False):
            return

        users = await self.get_kv_data("users", {})
        if not users:
            return

        max_delay = max(0, int(config.get("auto_sign_delay", 10)))
        for user_id, user_data in users.items():
            accounts = self._normalize_accounts(user_data)
            if not accounts:
                continue

            summaries: list[str] = []
            all_ok = True
            for index, entry in enumerate(accounts, start=1):
                if max_delay > 0:
                    await asyncio.sleep(random.uniform(0, max_delay))
                try:
                    ok, details = await self._do_sign_for_account(entry)
                    all_ok = all_ok and ok
                    detail_text = "\n".join(details[:8]) if details else "无详细信息"
                    summaries.append(f"【账号 {index}】\n{detail_text}")
                except Exception as e:
                    all_ok = False
                    logger.error(f"用户 {user_id} 的第 {index} 个账号自动签到失败: {e}")
                    summaries.append(f"【账号 {index}】\n❌ 失败：{str(e)}")

            self._store_accounts(user_data, accounts)
            users[user_id] = user_data
            header = "🎮 异环自动签到完成\n结果：成功" if all_ok else "⚠️ 异环自动签到完成\n结果：部分失败，请查看详情"
            msg = header
            if summaries:
                msg = f"{msg}\n\n" + "\n\n".join(summaries[:12])
            await self._send_private_message(user_id, user_data, msg)
        await self.put_kv_data("users", users)

    async def _set_pending(self, user_id: str, data: dict):
        pending = await self.get_kv_data("pending_login", {})
        pending[user_id] = {
            **data,
            "created_at": int(datetime.now().timestamp()),
        }
        await self.put_kv_data("pending_login", pending)

    async def _clear_pending(self, user_id: str):
        pending = await self.get_kv_data("pending_login", {})
        if user_id in pending:
            del pending[user_id]
            await self.put_kv_data("pending_login", pending)

    @filter.command("ntehelp")
    async def ntehelp(self, event: AstrMessageEvent):
        yield event.plain_result(
            "异环签到插件帮助\n"
            "1. /ntepw <手机号> -> 下一条私聊消息发送密码完成登录\n"
            "2. /nteph <手机号> -> 获取验证码后，下一条私聊消息发送验证码完成登录\n"
            "3. /nte 立即签到全部已绑定账号\n"
            "4. /ntelist 查看当前绑定账号\n"
            "5. /ntelogout 解除全部绑定\n"
            "6. /ntelogout <序号> 删除指定账号绑定"
        )

    @filter.command("ntelist")
    async def ntelist(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /ntelist")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        users = await self.get_kv_data("users", {})
        existing_user_key = self._pick_existing_key(users, user_keys)
        user_data = users.get(existing_user_key) if existing_user_key else None
        if not user_data:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw 或 /nteph 登录")
            return

        if existing_user_key and existing_user_key != user_id:
            users[user_id] = users.pop(existing_user_key)
            user_data = users[user_id]
            await self.put_kv_data("users", users)

        accounts = self._normalize_accounts(user_data)
        if not accounts:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw 或 /nteph 登录")
            return

        summaries = "\n".join(self._format_account_brief(item, i) for i, item in enumerate(accounts, start=1))
        yield event.plain_result(f"当前共绑定 {len(accounts)} 个账号：\n{summaries}")

    @filter.command("ntepw")
    async def ntepw(self, event: AstrMessageEvent, phone: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /ntepw 登录，避免泄露隐私")
            return
        phone = phone.strip()
        if not self._valid_phone(phone):
            yield event.plain_result("手机号格式错误，请使用：/ntepw 13800138000")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        users = await self.get_kv_data("users", {})
        config = self._get_config()
        max_users = int(config.get("max_users", 20))
        existing_key = self._pick_existing_key(users, user_keys)
        if existing_key is None and max_users > 0 and len(users) >= max_users:
            yield event.plain_result(f"❌ 绑定失败：已达到最大用户数限制（{max_users}）")
            return

        await self._set_pending(user_id, {"mode": "password", "phone": phone})
        yield event.plain_result("已记录手机号，请直接回复密码（10分钟内有效）")

    @filter.command("nteph")
    async def nteph(self, event: AstrMessageEvent, phone: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /nteph 获取验证码，避免泄露隐私")
            return
        phone = phone.strip()
        if not self._valid_phone(phone):
            yield event.plain_result("手机号格式错误，请使用：/nteph 13800138000")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        users = await self.get_kv_data("users", {})
        config = self._get_config()
        max_users = int(config.get("max_users", 20))
        existing_key = self._pick_existing_key(users, user_keys)
        if existing_key is None and max_users > 0 and len(users) >= max_users:
            yield event.plain_result(f"❌ 绑定失败：已达到最大用户数限制（{max_users}）")
            return

        try:
            device_id = await asyncio.to_thread(nte.send_login_captcha, phone)
        except Exception as e:
            yield event.plain_result(f"发送验证码失败：{str(e)}")
            return

        await self._set_pending(
            user_id,
            {
                "mode": "sms",
                "phone": phone,
                "device_id": device_id,
            },
        )
        yield event.plain_result("验证码已发送，请直接回复验证码（10分钟内有效）")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.regex(r"^[^/].+")
    async def handle_pending_login_input(self, event: AstrMessageEvent):
        user_keys = self._build_user_keys(event)
        if not user_keys:
            return
        user_id = user_keys[0]
        pending = await self.get_kv_data("pending_login", {})
        pending_key = self._pick_existing_key(pending, user_keys)
        session = pending.get(pending_key) if pending_key else None
        if not session:
            return

        now_ts = int(datetime.now().timestamp())
        created_at = int(session.get("created_at", 0))
        if created_at <= 0 or now_ts - created_at > PENDING_EXPIRE_SECONDS:
            await self._clear_pending(pending_key or user_id)
            yield event.plain_result("登录流程已过期，请重新发送 /ntepw 或 /nteph")
            return

        content = event.get_message_str().strip()
        if not content:
            return
        # 防止命令文本（包含可能被去掉 "/" 的情况）被误当作密码或验证码
        if COMMAND_TEXT_RE.match(content):
            return

        mode = session.get("mode")
        phone = str(session.get("phone", "")).strip()
        device_id = str(session.get("device_id", "")).strip()

        try:
            if mode == "password":
                account = await asyncio.to_thread(nte.build_account_by_password, phone, content)
            elif mode == "sms":
                account = await asyncio.to_thread(nte.build_account_by_sms, phone, content, device_id)
            else:
                await self._clear_pending(pending_key or user_id)
                yield event.plain_result("登录状态异常，请重新发送 /ntepw 或 /nteph")
                return
        except Exception as e:
            retry_hint = "可直接重试发送密码" if mode == "password" else "可直接重试发送验证码"
            yield event.plain_result(f"登录失败：{str(e)}\n{retry_hint}")
            return

        users = await self.get_kv_data("users", {})
        existing_user_key = self._pick_existing_key(users, user_keys)
        if existing_user_key and existing_user_key != user_id:
            users[user_id] = users.pop(existing_user_key)
        user_data = users.get(user_id, {})
        accounts = self._normalize_accounts(user_data)
        new_entry = {
            "account": account,
            "phone": phone,
            "bound_at": datetime.now().isoformat(),
            "last_sign_at": None,
        }
        action, idx = self._upsert_account(accounts, new_entry)
        user_data.update(
            {
                "last_username": event.get_sender_name(),
                "platform_name": event.get_platform_name(),
                "umo": event.unified_msg_origin,
            }
        )
        self._store_accounts(user_data, accounts)
        users[user_id] = user_data
        await self.put_kv_data("users", users)
        await self._clear_pending(pending_key or user_id)
        summaries = "\n".join(self._format_account_brief(item, i) for i, item in enumerate(accounts, start=1))
        action_text = "已更新已有账号" if action == "updated" else "已新增绑定账号"
        yield event.plain_result(
            f"登录成功，{action_text}。\n当前共绑定 {len(accounts)} 个账号。\n"
            f"本次账号序号：{idx + 1}\n\n{summaries}\n\n发送 /nte 即可签到全部账号。"
        )

    @filter.command("ntelogout")
    async def ntelogout(self, event: AstrMessageEvent, index: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /ntelogout")
            return
        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        users = await self.get_kv_data("users", {})
        user_id = user_keys[0]
        existing_user_key = self._pick_existing_key(users, user_keys)
        if existing_user_key and existing_user_key != user_id:
            users[user_id] = users.pop(existing_user_key)
            existing_user_key = user_id
        changed = False
        message = "你当前没有绑定账号"

        if existing_user_key and existing_user_key in users:
            user_data = users[existing_user_key]
            accounts = self._normalize_accounts(user_data)
            raw_index = index.strip()
            if raw_index:
                if not raw_index.isdigit():
                    yield event.plain_result("序号格式错误，请使用 /ntelogout 1")
                    return
                target = int(raw_index)
                if target <= 0 or target > len(accounts):
                    yield event.plain_result(f"序号超出范围，当前共有 {len(accounts)} 个账号")
                    return
                removed = accounts.pop(target - 1)
                changed = True
                if accounts:
                    self._store_accounts(user_data, accounts)
                    users[existing_user_key] = user_data
                    message = (
                        f"已删除第 {target} 个账号绑定：{self._format_account_brief(removed, target)}\n"
                        f"剩余 {len(accounts)} 个账号。"
                    )
                else:
                    del users[existing_user_key]
                    message = "已删除最后一个账号绑定，并清空当前用户的登录信息"
            else:
                del users[existing_user_key]
                changed = True
                message = "已清除全部登录信息"

        if changed:
            await self.put_kv_data("users", users)

        pending = await self.get_kv_data("pending_login", {})
        pending_changed = False
        for user_id in user_keys:
            if user_id in pending:
                del pending[user_id]
                pending_changed = True
        if pending_changed:
            await self.put_kv_data("pending_login", pending)
            changed = True
        yield event.plain_result(message if changed else "你当前没有绑定账号")

    @filter.command("nte")
    async def nte_sign(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /nte 签到")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        users = await self.get_kv_data("users", {})
        existing_user_key = self._pick_existing_key(users, user_keys)
        user_data = users.get(existing_user_key) if existing_user_key else None
        if not user_data:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw 或 /nteph 登录")
            return

        if existing_user_key and existing_user_key != user_id:
            users[user_id] = users.pop(existing_user_key)
            user_data = users[user_id]

        accounts = self._normalize_accounts(user_data)
        if not accounts:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw 或 /nteph 登录")
            return

        yield event.plain_result(f"正在签到，请稍候...（共 {len(accounts)} 个账号）")
        all_ok = True
        summaries: list[str] = []
        for index, entry in enumerate(accounts, start=1):
            try:
                ok, details = await self._do_sign_for_account(entry)
                all_ok = all_ok and ok
                detail_text = "\n".join(details[:12]) if details else "无详细信息"
                summaries.append(f"【账号 {index}】\n{detail_text}")
            except Exception as e:
                all_ok = False
                summaries.append(f"【账号 {index}】\n❌ 签到失败：{str(e)}")

        self._store_accounts(user_data, accounts)
        users[user_id] = user_data
        await self.put_kv_data("users", users)
        detail_text = "\n\n".join(summaries[:12]) if summaries else "无详细信息"
        if all_ok:
            yield event.plain_result(f"✅ 签到完成\n\n{detail_text}")
        else:
            yield event.plain_result(f"⚠️ 签到完成，但存在失败项\n\n{detail_text}")
