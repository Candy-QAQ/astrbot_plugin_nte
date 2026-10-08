"""Small offline harness for testing the plugin without running AstrBot."""

import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def _identity_decorator(*args, **kwargs):
    return lambda function: function


class MemoryStar:
    """Model the copy and async boundaries of a persisted JSON key-value store."""

    def __init__(self, context):
        self.context = context
        self.storage = {}

    async def get_kv_data(self, key, default=None):
        await asyncio.sleep(0)
        return copy.deepcopy(self.storage.get(key, default))

    async def put_kv_data(self, key, value):
        await asyncio.sleep(0)
        self.storage[key] = copy.deepcopy(value)


class MessageEvent:
    def __init__(self, text="", sender="42", platform="test", group=None):
        self.text = text
        self.sender = sender
        self.platform = platform
        self.message_obj = SimpleNamespace(group_id=group)
        self.unified_msg_origin = f"{platform}:FriendMessage:{sender}"

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return "Test user"

    def get_platform_name(self):
        return self.platform

    def plain_result(self, text):
        return text


async def collect(generator):
    return [message async for message in generator]


def _unmocked_api(*args, **kwargs):
    raise AssertionError("The test must mock account APIs; network access is forbidden")


def load_nte_module(logger=None):
    """Load real HTTP helpers with an isolated AstrBot logger dependency."""
    # Keep real dependencies outside the temporary sys.modules snapshot.
    # cryptography resolves algorithm classes lazily and requires their identity
    # to stay consistent after the framework stubs have been removed.
    for dependency in (
        "requests",
        "cryptography.hazmat.primitives.padding",
        "cryptography.hazmat.primitives.ciphers",
    ):
        importlib.import_module(dependency)
    if logger is None:
        logger = Mock(spec=("info", "warning", "error", "exception", "debug"))
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    api.logger = logger
    astrbot.api = api
    spec = importlib.util.spec_from_file_location("_nte_http_tests", ROOT / "nte.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "astrbot": astrbot,
        "astrbot.api": api,
        spec.name: module,
    }):
        spec.loader.exec_module(module)
    return module


def load_plugin_module():
    """Load the real main.py with framework dependencies isolated to this import."""
    package_name = "_nte_pending_login_tests"
    package = ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    nte = ModuleType(f"{package_name}.nte")
    for api in (
        "send_login_captcha",
        "send_cloud_login_captcha",
        "build_account_by_password",
        "build_account_by_sms",
        "build_cloud_account_by_sms",
        "do_sign",
    ):
        setattr(nte, api, _unmocked_api)
    nte._dedup_list = lambda values: list(dict.fromkeys(values))
    package.nte = nte

    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    api.logger = Mock(spec=("info", "warning", "error", "exception", "debug"))
    api.AstrBotConfig = dict
    event = ModuleType("astrbot.api.event")
    event.AstrMessageEvent = MessageEvent
    event.filter = SimpleNamespace(
        command=_identity_decorator,
        regex=_identity_decorator,
        event_message_type=_identity_decorator,
        EventMessageType=SimpleNamespace(PRIVATE_MESSAGE="private"),
    )
    event.MessageChain = type(
        "MessageChain", (), {"message": lambda self, text: text}
    )
    star = ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = MemoryStar
    star.register = _identity_decorator
    config = ModuleType("astrbot.core.star.config")
    config.put_config = lambda **kwargs: None

    scheduler = ModuleType("apscheduler.schedulers.asyncio")
    scheduler.AsyncIOScheduler = type("Scheduler", (), {"running": False})
    cron = ModuleType("apscheduler.triggers.cron")
    cron.CronTrigger = lambda **kwargs: kwargs
    modules = {
        package_name: package,
        f"{package_name}.nte": nte,
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core": ModuleType("astrbot.core"),
        "astrbot.core.star": ModuleType("astrbot.core.star"),
        "astrbot.core.star.config": config,
        "apscheduler": ModuleType("apscheduler"),
        "apscheduler.schedulers": ModuleType("apscheduler.schedulers"),
        "apscheduler.schedulers.asyncio": scheduler,
        "apscheduler.triggers": ModuleType("apscheduler.triggers"),
        "apscheduler.triggers.cron": cron,
    }
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.main", ROOT / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    modules[spec.name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module
