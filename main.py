"""
AstrBot Plugin - 异环签到 (NTE Auto Sign)

Commands:
- ntepw <手机号> (private): 输入手机号后，下一条私聊消息输入密码完成登录
- nteph <手机号> (private): 获取验证码后，下一条私聊消息输入验证码完成登录
- nte (private): 立即签到
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
COMMAND_TEXT_RE = re.compile(r"^/?(nte|ntepw|nteph|ntelogout|ntehelp)(\s|$)", re.IGNORECASE)


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
            "auto_sign_delay": self.config.get("auto_sign_delay", 10),
            "max_users": self.config.get("max_users", 20),
        }

    async def initialize(self):
        logger.info("异环签到插件已加载")
        config = self._get_config()
        if config.get("auto_sign_enabled", False):
            self._start_auto_sign_job(config.get("auto_sign_hour", 9))
        if not self.scheduler.running:
            self.scheduler.start()

    async def terminate(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("异环签到插件已卸载")

    def _start_auto_sign_job(self, hour: int):
        hour = max(0, min(23, int(hour)))
        trigger = CronTrigger(hour=hour, minute=0)
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
        logger.info(f"异环自动签到任务已启动，每天 {hour:02d}:00 执行")

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

    async def _do_sign_for_user(self, user_data: dict) -> tuple[bool, list[str]]:
        account = copy.deepcopy(user_data.get("account") or {})
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
        user_data["account"] = account
        user_data["last_sign_at"] = datetime.now().isoformat()
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
            if max_delay > 0:
                await asyncio.sleep(random.uniform(0, max_delay))
            try:
                ok, details = await self._do_sign_for_user(user_data)
                users[user_id] = user_data
                if ok:
                    msg = "🎮 异环自动签到完成\n结果：成功"
                else:
                    msg = "⚠️ 异环自动签到完成\n结果：部分失败，请查看日志"
                if details:
                    msg = f"{msg}\n\n" + "\n".join(details[:8])
                await self._send_private_message(user_id, user_data, msg)
            except Exception as e:
                logger.error(f"用户 {user_id} 自动签到失败: {e}")
                await self._send_private_message(
                    user_id,
                    user_data,
                    f"❌ 异环自动签到失败：{str(e)}\n请重新使用 /ntepw 或 /nteph 登录",
                )
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
            "3. /nte 立即签到\n"
            "4. /ntelogout 解除绑定"
        )

    @filter.command("ntepw")
    async def ntepw(self, event: AstrMessageEvent, phone: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /ntepw 登录，避免泄露隐私")
            return
        phone = phone.strip()
        if not self._valid_phone(phone):
            yield event.plain_result("手机号格式错误，请使用：/ntepw 13800138000")
            return

        user_id = event.get_sender_id()
        users = await self.get_kv_data("users", {})
        config = self._get_config()
        max_users = int(config.get("max_users", 20))
        if user_id not in users and max_users > 0 and len(users) >= max_users:
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

        user_id = event.get_sender_id()
        users = await self.get_kv_data("users", {})
        config = self._get_config()
        max_users = int(config.get("max_users", 20))
        if user_id not in users and max_users > 0 and len(users) >= max_users:
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
        user_id = event.get_sender_id()
        pending = await self.get_kv_data("pending_login", {})
        session = pending.get(user_id)
        if not session:
            return

        now_ts = int(datetime.now().timestamp())
        created_at = int(session.get("created_at", 0))
        if created_at <= 0 or now_ts - created_at > PENDING_EXPIRE_SECONDS:
            await self._clear_pending(user_id)
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
                await self._clear_pending(user_id)
                yield event.plain_result("登录状态异常，请重新发送 /ntepw 或 /nteph")
                return
        except Exception as e:
            retry_hint = "可直接重试发送密码" if mode == "password" else "可直接重试发送验证码"
            yield event.plain_result(f"登录失败：{str(e)}\n{retry_hint}")
            return

        users = await self.get_kv_data("users", {})
        users[user_id] = {
            "account": account,
            "phone": phone,
            "bound_at": datetime.now().isoformat(),
            "last_username": event.get_sender_name(),
            "platform_name": event.get_platform_name(),
            "umo": event.unified_msg_origin,
        }
        await self.put_kv_data("users", users)
        await self._clear_pending(user_id)
        yield event.plain_result("登录成功，已绑定账号。发送 /nte 即可签到")

    @filter.command("ntelogout")
    async def ntelogout(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /ntelogout")
            return
        user_id = event.get_sender_id()
        users = await self.get_kv_data("users", {})
        changed = False
        if user_id in users:
            del users[user_id]
            await self.put_kv_data("users", users)
            changed = True
        pending = await self.get_kv_data("pending_login", {})
        if user_id in pending:
            del pending[user_id]
            await self.put_kv_data("pending_login", pending)
            changed = True
        if changed:
            yield event.plain_result("已清除登录信息")
        else:
            yield event.plain_result("你当前没有绑定账号")

    @filter.command("nte")
    async def nte_sign(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /nte 签到")
            return

        user_id = event.get_sender_id()
        users = await self.get_kv_data("users", {})
        user_data = users.get(user_id)
        if not user_data:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw 或 /nteph 登录")
            return

        yield event.plain_result("正在签到，请稍候...")
        try:
            ok, details = await self._do_sign_for_user(user_data)
            users[user_id] = user_data
            await self.put_kv_data("users", users)
            if ok:
                detail_text = "\n".join(details[:12]) if details else "无详细信息"
                yield event.plain_result(f"✅ 签到完成\n\n{detail_text}")
            else:
                detail_text = "\n".join(details[:12]) if details else "无详细信息"
                yield event.plain_result(f"⚠️ 签到完成，但存在失败项\n\n{detail_text}")
        except Exception as e:
            yield event.plain_result(f"❌ 签到失败：{str(e)}")
