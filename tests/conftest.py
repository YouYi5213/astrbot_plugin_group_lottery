"""测试夹具：注入最小可用的 astrbot 桩模块。

真实的 AstrBot 采用运行时注入，必须启动本体才能加载插件。为了在 CI / 本地
无 AstrBot 环境下也能验证插件逻辑，这里把插件实际用到的 astrbot API 用最小
实现替代，让 ``main.py`` 可以被正常 import 并驱动。
"""

from __future__ import annotations

import base64
import logging
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PARENT = PLUGIN_ROOT.parent
for _path in (str(PLUGIN_ROOT), str(PLUGIN_PARENT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

_DATA_DIR = PLUGIN_ROOT / "tests" / ".tmp" / "plugin_data"

#: 模拟「AstrBot 按链接下载图片」：测试往这里塞 URL → 图片字节。
#: 真实实现里 ``Image.convert_to_base64()`` 会走 MediaResolver 去下载。
URL_BYTES: dict[str, bytes] = {}


@pytest.fixture
def url_images():
    """让被测代码能从链接「下载」到图片，用完清空。"""
    URL_BYTES.clear()
    yield URL_BYTES
    URL_BYTES.clear()


# --------------------------------------------------------------- 装饰器桩


def _identity_decorator(*_args, **_kwargs):
    """把 ``@filter.command(...)`` 之类的装饰器退化成恒等装饰器。"""

    def decorator(func):
        return func

    return decorator


class _FilterStub:
    """替代 ``astrbot.api.event.filter``。"""

    EventMessageType = types.SimpleNamespace(ALL="ALL", GROUP_MESSAGE="GroupMessage")
    PlatformAdapterType = types.SimpleNamespace(ALL="ALL")
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    command = staticmethod(_identity_decorator)
    command_group = staticmethod(_identity_decorator)
    regex = staticmethod(_identity_decorator)
    event_message_type = staticmethod(_identity_decorator)
    platform_adapter_type = staticmethod(_identity_decorator)
    permission_type = staticmethod(_identity_decorator)
    on_astrbot_loaded = staticmethod(_identity_decorator)
    on_platform_loaded = staticmethod(_identity_decorator)
    llm_tool = staticmethod(_identity_decorator)


# ------------------------------------------------------------------ 组件桩


class Plain:
    """替代 ``message_components.Plain``。"""

    type = "plain"

    def __init__(self, text: str = "") -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"Plain({self.text!r})"


class At:
    """替代 ``message_components.At``。"""

    type = "at"

    def __init__(self, qq=None, **_kwargs) -> None:
        self.qq = qq

    def __repr__(self) -> str:
        return f"At({self.qq})"


class AtAll:
    """替代 ``message_components.AtAll``（OneBot 下序列化为 at/all）。"""

    type = "at"

    def __init__(self, **_kwargs) -> None:
        self.qq = "all"

    def __repr__(self) -> str:
        return "AtAll()"


class Image:
    """替代 ``message_components.Image``。

    ``convert_to_base64`` 按真实行为返回纯 base64：本地文件就读文件，网络图走
    ``URL_BYTES`` 这个由测试填充的注册表（模拟 MediaResolver 下载）。
    """

    type = "image"

    def __init__(
        self,
        file: str = "",
        url: str = "",
        path: str = "",
        **_kwargs,
    ) -> None:
        self.file = file
        self.url = url
        self.path = path

    @classmethod
    def fromFileSystem(cls, path) -> Image:
        target = Path(path)
        return cls(file=target.resolve(strict=False).as_uri(), path=str(target))

    @classmethod
    def fromURL(cls, url: str) -> Image:
        return cls(file=url, url=url)

    @classmethod
    def fromBase64(cls, data: str) -> Image:
        return cls(file=f"base64://{data}")

    async def convert_to_base64(self) -> str:
        """按真实语义返回**不带前缀**的纯 base64。"""
        if self.path:
            return base64.b64encode(Path(self.path).read_bytes()).decode()
        if self.file.startswith("base64://"):
            return self.file[len("base64://") :]
        return base64.b64encode(URL_BYTES.get(self.url, b"")).decode()

    def __repr__(self) -> str:
        return f"Image({self.file!r})"


class MessageChain:
    """替代 ``MessageChain``。"""

    def __init__(self, chain=None, **_kwargs) -> None:
        self.chain = list(chain or [])

    def message(self, text: str):
        self.chain.append(Plain(text))
        return self


class MessageEventResult:
    """替代 ``MessageEventResult``。"""

    def __init__(self, chain=None, **_kwargs) -> None:
        self.chain = list(chain or [])


# ------------------------------------------------------------------ 核心桩


class AstrBotConfig(dict):
    """替代 ``AstrBotConfig``。"""

    def save_config(self) -> None:
        return None


class Star:
    """替代 ``astrbot.api.star.Star``。"""

    def __init__(self, context=None, config=None) -> None:
        self.context = context
        if config is not None:
            self.config = config

    async def terminate(self) -> None:
        return None


class Context:
    """替代 ``astrbot.api.star.Context``。"""

    def __init__(self) -> None:
        self.platform_manager = types.SimpleNamespace(platform_insts=[])
        self.sent: list[tuple[str, MessageChain]] = []
        self.web_apis: list[tuple] = []
        # 测试可用：让私聊投递失败，模拟「未加好友 / 被风控」
        self.fail_private = False
        # 测试可用：让群消息投递失败，模拟适配器没找到目标群
        self.fail_group = False

    async def send_message(self, session, message_chain) -> bool:
        if self.fail_private and "FriendMessage" in str(session):
            return False
        if self.fail_group and "GroupMessage" in str(session):
            return False
        self.sent.append((str(session), message_chain))
        return True

    def register_web_api(self, route, handler, methods, desc) -> None:
        self.web_apis.append((route, handler, methods, desc))


class StarTools:
    """替代 ``astrbot.api.star.StarTools``。"""

    @staticmethod
    def get_data_dir(plugin_name: str | None = None) -> Path:
        target = _DATA_DIR / (plugin_name or "unknown")
        target.mkdir(parents=True, exist_ok=True)
        return target


class _QueryStub(dict):
    """替代 ``PluginMultiDict``：支持 ``get(key, default, type=...)``。"""

    def get(self, key, default=None, type=None):
        value = dict.get(self, key, default)
        if type is None or value is None:
            return value
        try:
            return type(value)
        except (TypeError, ValueError):
            return default


class _RequestStub:
    """替代 ``astrbot.api.web.request``。"""

    def __init__(self) -> None:
        self.query = _QueryStub()
        self._json = {}

    async def json(self, default=None):
        return self._json or default


def _install() -> None:
    """把桩模块注册进 ``sys.modules``（已安装则跳过）。"""
    if "astrbot" in sys.modules:
        return

    astrbot = types.ModuleType("astrbot")
    astrbot.logger = logging.getLogger("astrbot.test")

    api = types.ModuleType("astrbot.api")
    api.logger = astrbot.logger
    api.AstrBotConfig = AstrBotConfig

    api_event = types.ModuleType("astrbot.api.event")
    api_event.filter = _FilterStub
    api_event.AstrMessageEvent = object
    api_event.MessageChain = MessageChain
    api_event.MessageEventResult = MessageEventResult
    api_event.CommandResult = MessageEventResult

    api_star = types.ModuleType("astrbot.api.star")
    api_star.Context = Context
    api_star.Star = Star
    api_star.StarTools = StarTools
    api_star.register = _identity_decorator

    api_components = types.ModuleType("astrbot.api.message_components")
    api_components.Plain = Plain
    api_components.At = At
    api_components.AtAll = AtAll
    api_components.Image = Image

    api_web = types.ModuleType("astrbot.api.web")
    api_web.request = _RequestStub()
    api_web.json_response = lambda data=None, **_: data
    api_web.error_response = lambda message, **_: {
        "status": "error",
        "message": message,
    }

    api.all = types.ModuleType("astrbot.api.all")

    astrbot.api = api
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": api_event,
            "astrbot.api.star": api_star,
            "astrbot.api.message_components": api_components,
            "astrbot.api.web": api_web,
            "astrbot.api.all": api.all,
        },
    )


_install()
