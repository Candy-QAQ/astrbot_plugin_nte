"""
AstrBot Plugin - 异环签到 (NTE Auto Sign)

Commands:
- ntepw <手机号> (private): 输入手机号后，下一条私聊消息输入密码完成登录
- nteph <手机号> (private): 获取验证码后，下一条私聊消息输入验证码完成登录
- nteyun <手机号> (private): 获取云异环验证码后，下一条私聊消息输入验证码完成登录
- nte (private): 立即签到
- ntelist (private): 查看当前已绑定账号
- ntelogout (private): 解除绑定
- ntecancel (private): 取消当前登录流程
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
import copy
import random
import re
import uuid

from . import nte

PLUGIN_NAME = "astrbot_plugin_nte"
PENDING_EXPIRE_SECONDS = 600
SMS_COOLDOWN_SECONDS = 60
SMS_DAILY_LIMIT = 10
PHONE_RE = re.compile(r"^1\d{10}$")
SMS_CODE_RE = re.compile(r"^[0-9]{4,8}$")
COMMAND_TEXT_RE = re.compile(
    r"^/?(nte|ntepw|nteph|nteyun|ntelist|ntelogout|ntecancel|ntehelp)(\s|$)",
    re.IGNORECASE,
)


@register(PLUGIN_NAME, "AstrBot", "异环自动签到插件", "1.1.1")
class NTEPlugin(Star):
    """异环签到插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self._state_lock = asyncio.Lock()
        self._login_requests: dict[str, str] = {}
        self._signing_accounts: dict[tuple[str, str], str] = {}
        self._user_key_aliases: dict[str, str] = {}
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
            name="最大绑定聊天用户数",
            key="max_users",
            value=20,
            description="0 为不限制；单个聊天用户可绑定多个账号",
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
                if not self._has_usable_credentials(account):
                    continue
                normalized.append(
                    {
                        "account": account,
                        "phone": str(item.get("phone", "")).strip(),
                        "kind": item.get("kind") or self._account_kind(account),
                        "bound_at": item.get("bound_at") or datetime.now().isoformat(),
                        "last_sign_at": item.get("last_sign_at"),
                    }
                )
        if normalized:
            return self._merge_accounts_by_phone(normalized)

        legacy_account = copy.deepcopy(user_data.get("account") or {})
        if self._has_usable_credentials(legacy_account):
            return [
                {
                    "account": legacy_account,
                    "phone": str(user_data.get("phone", "")).strip(),
                    "kind": self._account_kind(legacy_account),
                    "bound_at": user_data.get("bound_at") or datetime.now().isoformat(),
                    "last_sign_at": user_data.get("last_sign_at"),
                }
            ]
        return []

    @staticmethod
    def _has_usable_credentials(account: dict) -> bool:
        if not isinstance(account, dict) or not account:
            return False
        if str(account.get("refreshToken") or "").strip():
            return True
        return bool(
            str(account.get("cloudToken") or "").strip()
            and str(account.get("cloudUserId") or "").strip()
        )

    @staticmethod
    def _account_kind(account: dict) -> str:
        has_tajiduo = bool(str(account.get("refreshToken") or "").strip())
        has_cloud = bool(
            str(account.get("cloudToken") or "").strip()
            and str(account.get("cloudUserId") or "").strip()
        )
        if has_tajiduo and has_cloud:
            return "both"
        if has_cloud:
            return "cloud"
        return "tajiduo"

    def _store_accounts(self, user_data: dict, accounts: list[dict]):
        accounts = self._merge_accounts_by_phone(accounts)
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

    def _merge_account_entries(self, base_entry: dict, incoming_entry: dict) -> dict:
        base = copy.deepcopy(base_entry or {})
        incoming = copy.deepcopy(incoming_entry or {})
        base_account = base.get("account") or {}
        incoming_account = incoming.get("account") or {}

        merged_account = copy.deepcopy(base_account)
        for key, value in incoming_account.items():
            if value in (None, "", []):
                continue
            if key == "roleIds":
                merged_account[key] = nte._dedup_list((merged_account.get(key) or []) + value)
                continue
            if key == "roles":
                existing_roles = merged_account.get("roles") or []
                merged_account[key] = self._merge_role_lists(existing_roles, value)
                continue
            merged_account[key] = value

        base["account"] = merged_account
        base["kind"] = self._account_kind(merged_account)
        if incoming.get("phone"):
            base["phone"] = incoming.get("phone")
        elif not base.get("phone"):
            base["phone"] = ""
        base["bound_at"] = base.get("bound_at") or incoming.get("bound_at") or datetime.now().isoformat()
        base["last_sign_at"] = incoming.get("last_sign_at") or base.get("last_sign_at")
        return base

    @staticmethod
    def _merge_role_lists(left: list, right: list) -> list:
        merged: list[dict] = []
        seen: set[str] = set()
        for source in (left or [], right or []):
            if not isinstance(source, dict):
                continue
            role_id = str(source.get("roleId") or source.get("role_id") or "").strip()
            if not role_id or role_id in seen:
                continue
            role = {"roleId": role_id}
            role_name = str(source.get("roleName") or source.get("name") or "").strip()
            if role_name:
                role["roleName"] = role_name
            server_name = str(source.get("serverName") or "").strip()
            if server_name:
                role["serverName"] = server_name
            merged.append(role)
            seen.add(role_id)
        return merged

    def _merge_accounts_by_phone(self, accounts: list[dict]) -> list[dict]:
        merged: list[dict] = []
        for entry in accounts:
            phone = str(entry.get("phone") or "").strip()
            if phone:
                existing_idx = next(
                    (idx for idx, item in enumerate(merged) if str(item.get("phone") or "").strip() == phone),
                    None,
                )
                if existing_idx is not None:
                    merged[existing_idx] = self._merge_account_entries(merged[existing_idx], entry)
                    continue
            merged.append(entry)
        return merged

    def _upsert_account(self, accounts: list[dict], new_entry: dict) -> tuple[str, int]:
        new_account = new_entry.get("account") or {}
        new_uid = str(new_account.get("uid") or "").strip()
        new_cloud_uid = str(new_account.get("cloudUserId") or "").strip()
        new_game_id = str(new_account.get("gameId") or "").strip()
        new_phone = str(new_entry.get("phone") or "").strip()

        for idx, item in enumerate(accounts):
            account = item.get("account") or {}
            uid = str(account.get("uid") or "").strip()
            cloud_uid = str(account.get("cloudUserId") or "").strip()
            game_id = str(account.get("gameId") or "").strip()
            phone = str(item.get("phone") or "").strip()
            if new_phone and phone == new_phone:
                accounts[idx] = self._merge_account_entries(item, new_entry)
                return "updated", idx
            if new_uid and uid == new_uid and new_game_id and game_id == new_game_id:
                accounts[idx] = self._merge_account_entries(item, new_entry)
                return "updated", idx
            if new_cloud_uid and cloud_uid == new_cloud_uid:
                accounts[idx] = self._merge_account_entries(item, new_entry)
                return "updated", idx

        accounts.append(new_entry)
        return "added", len(accounts) - 1

    def _format_account_brief(self, entry: dict, index: int) -> str:
        account = entry.get("account") or {}
        kind = str(entry.get("kind") or self._account_kind(account))
        kind_text = {"tajiduo": "塔吉多", "cloud": "云异环", "both": "塔吉多+云异环"}.get(kind, kind)
        uid = str(account.get("uid") or "").strip()
        cloud_uid = str(account.get("cloudUserId") or "").strip()
        phone = str(entry.get("phone") or "").strip()
        phone_tail = phone[-4:] if len(phone) >= 4 else phone
        parts = [f"类型={kind_text}"]
        if uid:
            parts.append(f"uid={uid}")
        if cloud_uid:
            parts.append(f"cloudUid={cloud_uid}")
        if phone_tail:
            parts.append(f"手机号尾号={phone_tail}")
        last_sign_at = entry.get("last_sign_at")
        if last_sign_at:
            parts.append(f"上次签到={str(last_sign_at)[:19]}")
        return " | ".join(parts)

    @staticmethod
    def _display_kind(kind: str) -> str:
        return {
            "tajiduo": "塔吉多",
            "cloud": "云异环",
            "both": "塔吉多 + 云异环",
        }.get(kind, kind)

    @staticmethod
    def _format_datetime_text(value) -> str:
        text = str(value or "").strip()
        if not text:
            return "无"
        return text[:19].replace("T", " ")

    @staticmethod
    def _format_reward_text(text: str) -> str:
        text = str(text or "").strip()
        match = re.fullmatch(r"(.+?)x(\d+)", text)
        if match:
            return f"{match.group(1)} ×{match.group(2)}"
        return text

    @staticmethod
    def _format_cloud_duration(text: str) -> str:
        text = str(text or "").strip()
        if not text or text == "未知":
            return text or "未知"
        hour_match = re.search(r"(\d+)小时", text)
        minute_match = re.search(r"(\d+)分钟", text)
        hours = int(hour_match.group(1)) if hour_match else 0
        minutes = int(minute_match.group(1)) if minute_match else 0
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes or not parts:
            parts.append(f"{minutes}m")
        return "".join(parts)

    @staticmethod
    def _extract_role_display(role_label: str) -> str:
        text = str(role_label or "").strip()
        if not text:
            return "未知角色"
        if text.startswith("角色"):
            text = text[2:]
        match = re.fullmatch(r"(.+?)\((\d+)\)", text)
        if match:
            return match.group(1).strip() or f"角色{match.group(2)}"
        if text.isdigit():
            return f"角色{text}"
        return text

    def _parse_detail_lines(self, details: list[str]) -> dict:
        result = {
            "community": None,
            "games": [],
            "cloud": None,
            "errors": [],
            "other": [],
        }

        for line in details:
            text = str(line or "").strip()
            if not text:
                continue

            community = re.match(r"账号[^：]+：(.+)$", text)
            if community:
                message = community.group(1).strip()
                ok = "失败" not in message
                if message.startswith("社区"):
                    message = message[2:].strip()
                result["community"] = f"社区  {'✅' if ok else '❌'} {message}"
                continue

            game = re.match(r"(.+?)签到(成功|失败)：(.+)$", text)
            if game and game.group(1).startswith("角色"):
                role_name = self._extract_role_display(game.group(1))
                ok = game.group(2) == "成功"
                message = game.group(3).strip()
                reward = ""
                reward_match = re.search(r"今日道具：([^，；]+)", message)
                if reward_match:
                    reward = f" — {self._format_reward_text(reward_match.group(1))}"
                elif message:
                    cleaned = re.sub(r"[（(]gameId=[^）)]*[）)]", "", message).strip("， ")
                    reward = f" — {cleaned}" if cleaned else ""
                result["games"].append(f"游戏  {'✅' if ok else '❌'} {role_name}{reward}")
                continue

            if text.startswith("云异环时长："):
                cloud = re.search(
                    r"剩余([^，]+)，免费([^，]+)，充值([^，]+)(?:，.*?待领取消息(\d+)个)?",
                    text,
                )
                if cloud:
                    remained = self._format_cloud_duration(cloud.group(1))
                    free = self._format_cloud_duration(cloud.group(2))
                    recharge = self._format_cloud_duration(cloud.group(3))
                    untreated = cloud.group(4) or "0"
                    result["cloud"] = (
                        f"云端  ⏱ 剩余 {remained}（免费 {free} / 充值 {recharge}）待领取 {untreated}"
                    )
                else:
                    result["cloud"] = f"云端  ⏱ {text.removeprefix('云异环时长：')}"
                continue

            if "失败" in text or "失效" in text:
                result["errors"].append(f"错误  ❌ {text}")
            else:
                result["other"].append(text)

        return result

    def _format_sign_result(self, entry: dict, index: int, details: list[str], error: str | None = None) -> str:
        account = entry.get("account") or {}
        kind = str(entry.get("kind") or self._account_kind(account))
        uid = str(account.get("uid") or "").strip()
        cloud_uid = str(account.get("cloudUserId") or "").strip()
        phone = str(entry.get("phone") or "").strip()
        phone_tail = phone[-4:] if len(phone) >= 4 else phone or "未知"
        identity_label = "UID"
        identity = uid or cloud_uid or "未知"
        if not uid and cloud_uid:
            identity_label = "CloudUID"

        lines = [
            f"━━━━━━━━━━ 账号 {index} ━━━━━━━━━━",
            f"类型  {self._display_kind(kind)}",
            f"{identity_label}   {identity}  |  尾号 {phone_tail}",
            f"上次签到  {self._format_datetime_text(entry.get('last_sign_at'))}",
            "",
        ]

        if error:
            lines.append(f"错误  ❌ {error}")
            return "\n".join(lines)

        parsed = self._parse_detail_lines(details)
        if parsed["community"]:
            lines.append(parsed["community"])
        lines.extend(parsed["games"])
        if parsed["cloud"]:
            lines.append(parsed["cloud"])
        lines.extend(parsed["errors"])
        lines.extend(parsed["other"])
        if len(lines) == 5:
            lines.append("详情  无详细信息")
        return "\n".join(lines)

    async def _do_sign_for_account(self, entry: dict) -> tuple[bool, list[str]]:
        account = copy.deepcopy(entry.get("account") or {})
        if not self._has_usable_credentials(account):
            raise Exception("账号数据缺失，请重新登录")

        def _run_sign():
            output: list[str] = []
            sign_ok = nte.do_sign(account, output=output.append)
            lines = [line.strip() for text in output for line in text.splitlines() if line.strip()]
            return sign_ok, lines

        ok, details = await asyncio.to_thread(_run_sign)
        entry["account"] = account
        entry["last_sign_at"] = datetime.now().isoformat()
        return ok, details

    async def _auto_sign_all_users(self):
        config = self._get_config()
        if not config.get("auto_sign_enabled", False):
            return

        async with self._state_lock:
            users = copy.deepcopy(await self.get_kv_data("users", {}))
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
                if not await self._account_still_bound([user_id], entry):
                    continue
                try:
                    ok, details = await self._sign_bound_account([user_id], entry)
                    all_ok = all_ok and ok
                    summaries.append(self._format_sign_result(entry, index, details))
                except Exception as e:
                    all_ok = False
                    logger.error(f"用户 {user_id} 的第 {index} 个账号自动签到失败: {e}")
                    summaries.append(self._format_sign_result(entry, index, [], error=str(e)))

            _, current_user = await self._load_user([user_id])
            if not current_user or not summaries:
                continue
            header = "🎮 异环自动签到完成\n结果：成功" if all_ok else "⚠️ 异环自动签到完成\n结果：部分失败，请查看详情"
            msg = header
            if summaries:
                msg = f"{msg}\n\n" + "\n\n".join(summaries[:12])
            await self._send_private_message(user_id, current_user, msg)

    async def _load_user(self, user_keys: list[str]) -> tuple[str | None, dict | None]:
        async with self._state_lock:
            users = await self.get_kv_data("users", {})
            key = self._find_user_key(users, user_keys)
            if key is None:
                return None, None
            target = self._user_key_aliases.get(user_keys[0], user_keys[0])
            if key != target:
                users[target] = users.pop(key)
                self._user_key_aliases[key] = target
                for (owner, identity), token in list(self._signing_accounts.items()):
                    if owner == key:
                        self._signing_accounts[(target, identity)] = token
                key = target
                await self.put_kv_data("users", users)
            return key, copy.deepcopy(users[key])

    def _find_user_key(self, users: dict, user_keys: list[str]) -> str | None:
        # 自动签到只携带旧存储键；私聊兼容裸 sender_id 时不能跨平台追踪别名。
        keys = []
        for key in user_keys:
            alias = self._user_key_aliases.get(key)
            keys.append(alias if alias and (len(user_keys) == 1 or alias in user_keys) else key)
        keys.extend(user_keys)
        return self._pick_existing_key(users, keys)

    @staticmethod
    def _same_binding(current: dict, expected: dict) -> bool:
        return all(current.get(key) == expected.get(key) for key in ("phone", "bound_at", "account"))

    async def _account_still_bound(self, user_keys: list[str], entry: dict) -> bool:
        _, user_data = await self._load_user(user_keys)
        return bool(user_data and any(
            self._same_binding(item, entry) for item in self._normalize_accounts(user_data)
        ))

    async def _commit_sign_result(self, user_keys: list[str], before: dict, after: dict) -> bool:
        # 网络请求期间可能发生注销或重新登录，只更新仍然存在且凭据未变的绑定。
        async with self._state_lock:
            users = await self.get_kv_data("users", {})
            key = self._find_user_key(users, user_keys)
            if key is None:
                return False
            user_data = users[key]
            accounts = self._normalize_accounts(user_data)
            for index, current in enumerate(accounts):
                if self._same_binding(current, before):
                    accounts[index] = copy.deepcopy(after)
                    self._store_accounts(user_data, accounts)
                    users[key] = user_data
                    await self.put_kv_data("users", users)
                    return True
            return False

    async def _sign_bound_account(self, user_keys: list[str], entry: dict) -> tuple[bool, list[str]]:
        async with self._state_lock:
            users = await self.get_kv_data("users", {})
            key = self._find_user_key(users, user_keys)
            current = next((item for item in self._normalize_accounts(users[key])
                            if self._same_binding(item, entry)), None) if key else None
            if current is None:
                return False, ["账号绑定已变更，已跳过本次签到"]
            account = current.get("account") or {}
            identity = str(current.get("phone") or account.get("uid") or account.get("cloudUserId") or current.get("bound_at"))
            sign_key = (key, identity)
            if sign_key in self._signing_accounts:
                return False, ["该账号正在签到，请稍后查看结果"]
            sign_token = uuid.uuid4().hex
            self._signing_accounts[sign_key] = sign_token
            before = copy.deepcopy(current)
            entry.clear()
            entry.update(current)
        try:
            result = await self._do_sign_for_account(entry)
            await self._commit_sign_result(user_keys, before, entry)
            return result
        finally:
            async with self._state_lock:
                self._signing_accounts = {
                    key: token for key, token in self._signing_accounts.items() if token != sign_token
                }

    async def _clear_pending_unlocked(self, user_keys: list[str]) -> bool:
        pending = await self.get_kv_data("pending_login", {})
        changed = False
        for key in user_keys:
            if key in pending:
                del pending[key]
                changed = True
            if self._login_requests.pop(key, None) is not None:
                changed = True
        if changed:
            await self.put_kv_data("pending_login", pending)
        return changed

    async def _clear_pending(self, user_ids: str | list[str]) -> bool:
        keys = [user_ids] if isinstance(user_ids, str) else user_ids
        async with self._state_lock:
            return await self._clear_pending_unlocked(keys)

    async def _prepare_login(self, user_keys: list[str], mode: str, phone: str) -> tuple[str | None, str | None]:
        async with self._state_lock:
            users = await self.get_kv_data("users", {})
            max_users = int(self._get_config().get("max_users", 20))
            if self._pick_existing_key(users, user_keys) is None and max_users > 0 and len(users) >= max_users:
                return None, f"❌ 绑定失败：已达到最大用户数限制（{max_users}）"
            if mode in ("sms", "cloud_sms"):
                limits = await self.get_kv_data("captcha_limits", {})
                now = int(datetime.now().timestamp())
                today = datetime.now().date().isoformat()
                limit_keys = [f"user:{user_keys[0]}", f"phone:{phone}"]
                for key in limit_keys:
                    limit = limits.get(key, {})
                    remaining = SMS_COOLDOWN_SECONDS - (now - int(limit.get("last_sent", 0)))
                    if remaining > 0:
                        return None, f"验证码请求过于频繁，请 {remaining} 秒后重试；已收到的验证码仍可使用"
                    if limit.get("day") == today and int(limit.get("count", 0)) >= SMS_DAILY_LIMIT:
                        return None, f"今日验证码请求已达上限（{SMS_DAILY_LIMIT}次），请明天再试"
                # 预留次数包含失败请求，避免并发发码和反复失败绕过限流。
                limits = {key: value for key, value in limits.items()
                          if value.get("day") == today or now - int(value.get("last_sent", 0)) < SMS_COOLDOWN_SECONDS}
                for key in limit_keys:
                    previous = limits.get(key, {})
                    count = int(previous.get("count", 0)) if previous.get("day") == today else 0
                    limits[key] = {"day": today, "last_sent": now, "count": count + 1}
                await self.put_kv_data("captcha_limits", limits)
            await self._clear_pending_unlocked(user_keys)
            request_id = uuid.uuid4().hex
            self._login_requests[user_keys[0]] = request_id
            return request_id, None

    async def _set_pending(self, user_id: str, data: dict, request_id: str | None = None) -> bool:
        async with self._state_lock:
            if request_id is not None and self._login_requests.get(user_id) != request_id:
                return False
            request_id = request_id or uuid.uuid4().hex
            pending = await self.get_kv_data("pending_login", {})
            pending[user_id] = {**data, "created_at": int(datetime.now().timestamp()), "request_id": request_id}
            self._login_requests[user_id] = request_id
            await self.put_kv_data("pending_login", pending)
            return True

    async def _end_login_request(self, user_id: str, request_id: str) -> bool:
        async with self._state_lock:
            if self._login_requests.get(user_id) != request_id:
                return False
            del self._login_requests[user_id]
            return True

    async def _take_pending_input(self, user_keys: list[str], content: str) -> tuple[dict | None, str | None]:
        async with self._state_lock:
            pending = await self.get_kv_data("pending_login", {})
            key = self._pick_existing_key(pending, user_keys)
            session = pending.get(key) if key else None
            if not session:
                return None, None
            if not isinstance(session, dict):
                await self._clear_pending_unlocked(user_keys)
                return None, "登录状态异常，请重新发送 /ntepw、/nteph 或 /nteyun"
            mode = session.get("mode")
            is_code = bool(SMS_CODE_RE.fullmatch(content))
            try:
                created_at = int(session.get("created_at", 0))
            except (TypeError, ValueError):
                created_at = 0
            if created_at <= 0 or int(datetime.now().timestamp()) - created_at > PENDING_EXPIRE_SECONDS:
                await self._clear_pending_unlocked(user_keys)
                if mode in ("sms", "cloud_sms") and not is_code:
                    return None, None
                return None, "登录流程已过期，请重新发送 /ntepw、/nteph 或 /nteyun"
            if mode not in ("password", "sms", "cloud_sms"):
                await self._clear_pending_unlocked(user_keys)
                return None, "登录状态异常，请重新发送 /ntepw、/nteph 或 /nteyun"
            if mode in ("sms", "cloud_sms") and not is_code:
                return None, None
            # 消费本次输入后释放锁再访问网络，其他私聊不会重复提交凭据。
            session = copy.deepcopy(session)
            session["request_id"] = session.get("request_id") or uuid.uuid4().hex
            await self._clear_pending_unlocked(user_keys)
            self._login_requests[user_keys[0]] = session["request_id"]
            return session, None

    @filter.command("ntehelp")
    async def ntehelp(self, event: AstrMessageEvent):
        yield event.plain_result(
            "异环签到插件帮助\n"
            "1. /ntepw <手机号> -> 下一条私聊消息发送密码完成登录\n"
            "2. /nteph <手机号> -> 获取验证码后，下一条私聊消息发送验证码完成登录\n"
            "3. /nteyun <手机号> -> 获取云异环验证码后，下一条私聊消息发送验证码完成登录\n"
            "4. /nte 立即签到全部已绑定账号\n"
            "5. /nte <序号> 只签到指定账号\n"
            "6. /ntelist 查看当前绑定账号\n"
            "7. /ntelogout 解除全部绑定\n"
            "8. /ntelogout <序号> 删除指定账号绑定\n"
            "9. /ntecancel 取消当前登录，保留已绑定账号\n\n"
            "验证码仅接收4至8位数字；登录失败后需重新发起登录。\n"
            "验证码请求间隔至少60秒，每位用户及每个手机号每天最多10次。"
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
        _, user_data = await self._load_user(user_keys)
        if not user_data:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw、/nteph 或 /nteyun 登录")
            return

        accounts = self._normalize_accounts(user_data)
        if not accounts:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw、/nteph 或 /nteyun 登录")
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
        request_id, error = await self._prepare_login(user_keys, "password", phone)
        if error:
            yield event.plain_result(error)
            return

        if await self._set_pending(user_id, {"mode": "password", "phone": phone}, request_id):
            yield event.plain_result("已记录手机号，请直接回复密码（10分钟内有效）；发送 /ntecancel 可取消登录")

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
        request_id, error = await self._prepare_login(user_keys, "sms", phone)
        if error:
            yield event.plain_result(error)
            return

        try:
            device_id = await asyncio.to_thread(nte.send_login_captcha, phone)
        except Exception as e:
            if await self._end_login_request(user_id, request_id):
                yield event.plain_result(f"发送验证码失败：{str(e)}\n请稍后重新发送 /nteph <手机号>")
            return

        saved = await self._set_pending(
            user_id,
            {
                "mode": "sms",
                "phone": phone,
                "device_id": device_id,
            },
            request_id,
        )
        if saved:
            yield event.plain_result("验证码已发送，请直接回复4至8位数字验证码（10分钟内有效）；发送 /ntecancel 可取消登录")

    @filter.command("nteyun")
    async def nteyun(self, event: AstrMessageEvent, phone: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /nteyun 获取验证码，避免泄露隐私")
            return
        phone = phone.strip()
        if not self._valid_phone(phone):
            yield event.plain_result("手机号格式错误，请使用：/nteyun 13800138000")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        user_id = user_keys[0]
        request_id, error = await self._prepare_login(user_keys, "cloud_sms", phone)
        if error:
            yield event.plain_result(error)
            return

        try:
            device_id = await asyncio.to_thread(nte.send_cloud_login_captcha, phone)
        except Exception as e:
            if await self._end_login_request(user_id, request_id):
                yield event.plain_result(f"发送云异环验证码失败：{str(e)}\n请稍后重新发送 /nteyun <手机号>")
            return

        saved = await self._set_pending(
            user_id,
            {
                "mode": "cloud_sms",
                "phone": phone,
                "device_id": device_id,
            },
            request_id,
        )
        if saved:
            yield event.plain_result("云异环验证码已发送，请直接回复4至8位数字验证码（10分钟内有效）；发送 /ntecancel 可取消登录")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.regex(r"^[^/]")
    async def handle_pending_login_input(self, event: AstrMessageEvent):
        if not self._is_private(event):
            return
        content = event.get_message_str().strip()
        if not content or content.startswith("/") or COMMAND_TEXT_RE.match(content):
            return
        user_keys = self._build_user_keys(event)
        if not user_keys:
            return
        user_id = user_keys[0]
        session, error = await self._take_pending_input(user_keys, content)
        if error:
            yield event.plain_result(error)
            return
        if session is None:
            return

        mode = session.get("mode")
        phone = str(session.get("phone", "")).strip()
        device_id = str(session.get("device_id", "")).strip()
        request_id = session["request_id"]

        try:
            if mode == "password":
                account = await asyncio.to_thread(nte.build_account_by_password, phone, content)
            elif mode == "sms":
                account = await asyncio.to_thread(nte.build_account_by_sms, phone, content, device_id)
            elif mode == "cloud_sms":
                account = await asyncio.to_thread(nte.build_cloud_account_by_sms, phone, content, device_id)
        except Exception as e:
            command = {"password": "ntepw", "sms": "nteph", "cloud_sms": "nteyun"}[mode]
            if await self._end_login_request(user_id, request_id):
                yield event.plain_result(f"登录失败：{str(e)}\n本次登录已结束，请重新发送 /{command} <手机号>")
            return

        error = None
        async with self._state_lock:
            if self._login_requests.get(user_id) != request_id:
                return
            del self._login_requests[user_id]
            users = await self.get_kv_data("users", {})
            existing_user_key = self._pick_existing_key(users, user_keys)
            max_users = int(self._get_config().get("max_users", 20))
            if existing_user_key is None and max_users > 0 and len(users) >= max_users:
                error = f"❌ 绑定失败：已达到最大用户数限制（{max_users}），请稍后重新登录"
            else:
                if existing_user_key and existing_user_key != user_id:
                    users[user_id] = users.pop(existing_user_key)
                user_data = users.get(user_id, {})
                accounts = self._normalize_accounts(user_data)
                new_entry = {
                    "account": account,
                    "phone": phone,
                    "kind": self._account_kind(account),
                    "bound_at": datetime.now().isoformat(),
                    "last_sign_at": None,
                }
                action, idx = self._upsert_account(accounts, new_entry)
                user_data.update({
                    "last_username": event.get_sender_name(),
                    "platform_name": event.get_platform_name(),
                    "umo": event.unified_msg_origin,
                })
                self._store_accounts(user_data, accounts)
                users[user_id] = user_data
                await self.put_kv_data("users", users)
        if error:
            yield event.plain_result(error)
            return
        summaries = "\n".join(self._format_account_brief(item, i) for i, item in enumerate(accounts, start=1))
        action_text = "已更新已有账号" if action == "updated" else "已新增绑定账号"
        yield event.plain_result(
            f"登录成功，{action_text}。\n当前共绑定 {len(accounts)} 个账号。\n"
            f"本次账号序号：{idx + 1}\n\n{summaries}\n\n发送 /nte 即可签到全部账号。"
        )

    @filter.command("ntecancel")
    async def ntecancel(self, event: AstrMessageEvent):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /ntecancel")
            return
        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        changed = await self._clear_pending(user_keys)
        yield event.plain_result("已取消当前登录，已绑定账号仍然保留" if changed else "当前没有进行中的登录")

    @filter.command("ntelogout")
    async def ntelogout(self, event: AstrMessageEvent, index: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /ntelogout")
            return
        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        raw_index = index.strip()
        if raw_index and not raw_index.isdigit():
            yield event.plain_result("序号格式错误，请使用 /ntelogout 1")
            return
        error = None
        message = "你当前没有绑定账号"
        async with self._state_lock:
            users = await self.get_kv_data("users", {})
            key = self._pick_existing_key(users, user_keys)
            accounts = self._normalize_accounts(users[key]) if key else []
            target = int(raw_index) if raw_index else None
            if target is not None and (target <= 0 or target > len(accounts)):
                error = f"序号超出范围，当前共有 {len(accounts)} 个账号"
            else:
                if key:
                    if target is None:
                        del users[key]
                        message = "已清除全部登录信息"
                    else:
                        removed = accounts.pop(target - 1)
                        if accounts:
                            self._store_accounts(users[key], accounts)
                            message = (
                                f"已删除第 {target} 个账号绑定：{self._format_account_brief(removed, target)}\n"
                                f"剩余 {len(accounts)} 个账号。"
                            )
                        else:
                            del users[key]
                            message = "已删除最后一个账号绑定，并清空当前用户的登录信息"
                    await self.put_kv_data("users", users)
                cancelled = await self._clear_pending_unlocked(user_keys)
                if not key and cancelled:
                    message = "已取消当前登录，你当前没有绑定账号"
        yield event.plain_result(error or message)

    @filter.command("nte")
    async def nte_sign(self, event: AstrMessageEvent, index: str = ""):
        if not self._is_private(event):
            yield event.plain_result("请在私聊中使用 /nte 签到")
            return

        user_keys = self._build_user_keys(event)
        if not user_keys:
            yield event.plain_result("无法识别当前用户，请稍后重试")
            return
        _, user_data = await self._load_user(user_keys)
        if not user_data:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw、/nteph 或 /nteyun 登录")
            return

        accounts = self._normalize_accounts(user_data)
        if not accounts:
            yield event.plain_result("你还未绑定账号，请先使用 /ntepw、/nteph 或 /nteyun 登录")
            return

        target_indexes = list(range(len(accounts)))
        index = str(index or "").strip()
        if index:
            if not index.isdigit():
                yield event.plain_result("序号格式错误，请使用 /nte 1")
                return
            target = int(index)
            if target < 1 or target > len(accounts):
                yield event.plain_result(f"序号超出范围，当前共有 {len(accounts)} 个账号。发送 /ntelist 查看列表")
                return
            target_indexes = [target - 1]

        if len(target_indexes) == 1 and index:
            yield event.plain_result(
                f"正在签到第 {target_indexes[0] + 1} 个账号，请稍候...\n"
                f"{self._format_account_brief(accounts[target_indexes[0]], target_indexes[0] + 1)}"
            )
        else:
            yield event.plain_result(f"正在签到，请稍候...（共 {len(accounts)} 个账号）")

        all_ok = True
        summaries: list[str] = []
        for target_index in target_indexes:
            entry = accounts[target_index]
            account_number = target_index + 1
            try:
                ok, details = await self._sign_bound_account(user_keys, entry)
                all_ok = all_ok and ok
                summaries.append(self._format_sign_result(entry, account_number, details))
            except Exception as e:
                all_ok = False
                summaries.append(self._format_sign_result(entry, account_number, [], error=str(e)))

        detail_text = "\n\n".join(summaries[:12]) if summaries else "无详细信息"
        if all_ok:
            yield event.plain_result(f"✅ 签到完成\n\n{detail_text}")
        else:
            yield event.plain_result(f"⚠️ 签到完成，但存在失败项\n\n{detail_text}")
