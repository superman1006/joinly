import re
from typing import ClassVar, Protocol

from playwright.async_api import Page

from joinly.types import (
    MeetingChatHistory,
    MeetingParticipant,
    ProviderNotSupportedError,
)


class BrowserPlatformController(Protocol):
    """通过浏览器控制会议交互的协议。

    定义加入会议、与会中交互以及离开会议所使用的接口。
    """

    url_pattern: ClassVar[re.Pattern[str]]

    @property
    def active_speaker(self) -> str | None:
        """获取会议中当前发言人的名称。

        返回:
            str | None: 发言人名称；若不可得则为 None。
        """
        ...

    async def join(
        self, page: Page, url: str, name: str, passcode: str | None = None
    ) -> None:
        """加入会议。

        参数:
            page: 用于交互的 Playwright Page 对象。
            url: 要加入的会议 URL。
            name: 在会议中使用的显示名称。
            passcode: 会议密码（若需要）。
        """
        ...

    async def leave(self, page: Page) -> None:
        """离开当前会议。

        参数:
            page: 用于交互的 Playwright Page 对象。
        """
        ...

    async def send_chat_message(self, page: Page, message: str) -> None:
        """向会议发送聊天消息。

        参数:
            page: 用于交互的 Playwright Page 对象。
            message: 要发送的消息内容。
        """
        ...

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:
        """获取会议的聊天消息历史。

        参数:
            page: 用于交互的 Playwright Page 对象。

        返回:
            MeetingChatHistory: 会议的聊天历史。
        """
        ...

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """获取会议参与者列表。

        参数:
            page: 用于交互的 Playwright Page 对象。

        返回:
            list[MeetingParticipant]: 会议中的参与者列表。
        """
        ...

    async def mute(self, page: Page) -> None:
        """在会议中将自己静音。

        参数:
            page: 用于交互的 Playwright Page 对象。
        """
        ...

    async def unmute(self, page: Page) -> None:
        """在会议中取消静音。

        参数:
            page: 用于交互的 Playwright Page 对象。
        """
        ...

    async def share_screen(self, page: Page) -> None:
        """开始在会议中共享屏幕。

        参数:
            page: 用于交互的 Playwright Page 对象。
        """
        ...

    async def stop_sharing(self, page: Page) -> None:
        """停止在会议中共享屏幕。

        参数:
            page: 用于交互的 Playwright Page 对象。
        """
        ...


class BaseBrowserPlatformController(BrowserPlatformController):
    """各具体会议平台的浏览器控制器基类。"""

    url_pattern: ClassVar[re.Pattern[str]] = re.compile(r"^$")

    @property
    def active_speaker(self) -> str | None:
        """获取会议中当前发言人的名称。"""
        return None

    async def join(
        self,
        page: Page,  # noqa: ARG002
        url: str,  # noqa: ARG002
        name: str,  # noqa: ARG002
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """在指定 URL 加入会议。"""
        msg = "Provider does not support joining meetings."
        raise ProviderNotSupportedError(msg)

    async def leave(self, page: Page) -> None:  # noqa: ARG002
        """离开当前会议。"""
        msg = "Provider does not support leaving meetings."
        raise ProviderNotSupportedError(msg)

    async def send_chat_message(self, page: Page, message: str) -> None:  # noqa: ARG002
        """在会议中发送聊天消息。"""
        msg = "Provider does not support sending chat messages."
        raise ProviderNotSupportedError(msg)

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:  # noqa: ARG002
        """获取会议的聊天历史。"""
        msg = "Provider does not support retrieving chat history."
        raise ProviderNotSupportedError(msg)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:  # noqa: ARG002
        """获取会议参与者列表。"""
        msg = "Provider does not support retrieving participants."
        raise ProviderNotSupportedError(msg)

    async def mute(self, page: Page) -> None:  # noqa: ARG002
        """在会议中将自己静音。"""
        msg = "Provider does not support muting."
        raise ProviderNotSupportedError(msg)

    async def unmute(self, page: Page) -> None:  # noqa: ARG002
        """在会议中取消静音。"""
        msg = "Provider does not support unmuting."
        raise ProviderNotSupportedError(msg)

    async def share_screen(self, page: Page) -> None:  # noqa: ARG002
        """开始在会议中共享屏幕。"""
        msg = "Provider does not support screen sharing."
        raise ProviderNotSupportedError(msg)

    async def stop_sharing(self, page: Page) -> None:  # noqa: ARG002
        """停止在会议中共享屏幕。"""
        msg = "Provider does not support stopping screen share."
        raise ProviderNotSupportedError(msg)
