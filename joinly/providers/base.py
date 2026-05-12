from joinly.core import MeetingProvider
from joinly.types import (
    ActionAnimation,
    MeetingChatHistory,
    MeetingParticipant,
    ProviderNotSupportedError,
    UIUpdate,
)


class BaseMeetingProvider(MeetingProvider):
    """会议提供方的基类。"""

    async def join(
        self,
        url: str | None = None,  # noqa: ARG002
        name: str | None = None,  # noqa: ARG002
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """在指定 URL 加入会议。"""
        msg = "Provider does not support joining meetings."
        raise ProviderNotSupportedError(msg)

    async def leave(self) -> None:
        """离开当前会议。"""
        msg = "Provider does not support leaving meetings."
        raise ProviderNotSupportedError(msg)

    async def send_chat_message(self, message: str) -> None:  # noqa: ARG002
        """在会议中发送聊天消息。"""
        msg = "Provider does not support sending chat messages."
        raise ProviderNotSupportedError(msg)

    async def get_chat_history(self) -> MeetingChatHistory:
        """获取会议的聊天消息历史。"""
        msg = "Provider does not support retrieving chat history."
        raise ProviderNotSupportedError(msg)

    async def get_participants(self) -> list[MeetingParticipant]:
        """获取会议参与者列表。"""
        msg = "Provider does not support retrieving participants."
        raise ProviderNotSupportedError(msg)

    async def mute(self) -> None:
        """在会议中将自己静音。"""
        msg = "Provider does not support muting."
        raise ProviderNotSupportedError(msg)

    async def unmute(self) -> None:
        """在会议中取消静音。"""
        msg = "Provider does not support unmuting."
        raise ProviderNotSupportedError(msg)

    async def share_screen(self, url: str) -> None:  # noqa: ARG002
        """开始在会议中共享屏幕。"""
        msg = "Provider does not support screen sharing."
        raise ProviderNotSupportedError(msg)

    async def stop_sharing(self) -> None:
        """停止在会议中共享屏幕。"""
        msg = "Provider does not support stopping screen share."
        raise ProviderNotSupportedError(msg)

    async def set_animation(self, animation: ActionAnimation | None) -> None:
        """在摄像头画面上设置动作动画。"""

    async def update_ui(self, update: UIUpdate) -> None:
        """更新会议提供方上的 UI。"""
