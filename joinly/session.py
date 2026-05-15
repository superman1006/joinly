"""会议会话编排层（MeetingSession）。

在 ``SessionContainer`` 组装好各组件后，本模块提供面向 MCP 工具的高层 API：
加入/离开会议、朗读、发聊天、截图、共享屏幕等。

``speak_text`` 会同时触发 TTS 与会议聊天框同步（``_echo_to_chat``），便于飞书等
场景下参会者看到文字记录。
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager

from joinly.core import (
    MeetingProvider,
    SpeechController,
    TranscriptionController,
    VideoReader,
)
from joinly.types import (
    ActionAnimation,
    MeetingChatHistory,
    MeetingParticipant,
    SpeechInterruptedError,
    Transcript,
    UIUpdate,
    VideoSnapshot,
)
from joinly.utils.clock import Clock
from joinly.utils.events import EventBus, EventType

logger = logging.getLogger(__name__)


class MeetingSession:
    """编排会议相关操作的核心类。

    持有 ``MeetingProvider``、转写/语音控制器与 ``EventBus``，是 MCP 工具与底层
    组件之间的唯一门面（Facade）。
    """

    def __init__(
        self,
        meeting_provider: MeetingProvider,
        transcription_controller: TranscriptionController,
        speech_controller: SpeechController,
        video_reader: VideoReader,
    ) -> None:
        """初始化会议会话。

        参数:
            meeting_provider (MeetingProvider): 使用的会议提供方。
            transcription_controller (TranscriptionController): 管理转写的控制器。
            speech_controller (SpeechController): 管理语音输出的控制器。
            video_reader (VideoReader): 管理视频的控制器。
        """
        self._meeting_provider = meeting_provider
        self._transcription_controller = transcription_controller
        self._speech_controller = speech_controller
        self._video_reader = video_reader
        self._clock: Clock | None = None
        self._transcript: Transcript | None = None
        self._event_bus = EventBus()
        self._is_muted: bool = False
        # 持有 speak_text 期间发起的聊天发送任务引用，避免被 GC
        self._chat_echo_tasks: set[asyncio.Task[None]] = set()

    @property
    def transcript(self) -> Transcript:
        """返回当前会议的转写内容。"""
        if self._transcript is None:
            msg = "Not joined any meeting, cannot access transcript."
            raise RuntimeError(msg)
        return self._transcript

    @property
    def meeting_seconds(self) -> float:
        """返回当前会议时长（秒）。"""
        if self._clock is None:
            msg = "Not joined any meeting, cannot access meeting duration."
            raise RuntimeError(msg)
        return self._clock.now_s

    def subscribe(
        self, event_type: EventType, handler: Callable[[], Coroutine[None, None, None]]
    ) -> Callable[[], None]:
        """为转写相关事件添加监听。

        参数:
            event_type (EventType): 要监听的事件类型。
            handler: 可调用对象。

        返回:
            用于移除该监听器的可调用对象。
        """
        return self._event_bus.subscribe(event_type, handler)

    async def join_meeting(
        self,
        meeting_url: str | None = None,
        participant_name: str | None = None,
        passcode: str | None = None,
    ) -> None:
        """使用给定 URL 加入会议。

        参数:
            meeting_url (str | None): 要加入的会议 URL。是否必填取决于会议提供方。
            participant_name (str | None): 参与者显示名称。默认使用会话中的名称。
            passcode (str | None): 会议密码或通行码（若需要）。
        """
        await self._meeting_provider.join(meeting_url, participant_name, passcode)
        self._clock = Clock()
        self._transcript = Transcript()

        _unsubscribe: Callable[[], None] | None = None

        async def unmute_on_start() -> None:
            """会议开始后取消静音。"""
            if _unsubscribe is not None:
                _unsubscribe()
            with contextlib.suppress(Exception):
                await self._meeting_provider.unmute()

        _unsubscribe = self._event_bus.subscribe("segment", unmute_on_start)

        await self._transcription_controller.start(
            self._clock, self._transcript, self._event_bus
        )
        await self._speech_controller.start(
            self._clock, self._transcript, self._event_bus
        )

    async def leave_meeting(self) -> None:
        """离开当前会议。"""
        await self._meeting_provider.leave()
        await self._transcription_controller.stop()
        await self._speech_controller.stop()

    async def speak_text(self, text: str) -> None:
        """使用 TTS 朗读给定文本，同时把同一段文本发送到会议聊天框。

        静音状态下跳过 TTS 播放（避免 tts_active_event 阻塞 STT），仅发聊天消息。
        聊天发送以并发任务方式 fire-and-forget 触发，失败仅记日志，不影响 TTS。
        如果语音被打断（SpeechInterruptedError），聊天任务保留运行直至完成或失败。

        参数:
            text (str): 要朗读的文本。
        """
        if text.strip():
            task = asyncio.create_task(
                self._echo_to_chat(text), name="speak_text_chat_echo"
            )
            self._chat_echo_tasks.add(task)
            task.add_done_callback(self._chat_echo_tasks.discard)

        if self._is_muted:
            logger.debug("当前处于静音状态，跳过 TTS 播放")
            return

        try:
            await self._speech_controller.speak_text(text)
        except SpeechInterruptedError:
            await self.set_animation("interrupted")
            await self.set_animation(None)
            raise

    async def _echo_to_chat(self, text: str) -> None:
        """把 TTS 文本同步发送到会议聊天框，异常仅记日志。"""
        try:
            await self._meeting_provider.send_chat_message(text)
        except Exception:  # noqa: BLE001
            logger.warning("将朗读文本同步到聊天框失败", exc_info=True)

    async def send_chat_message(self, message: str) -> None:
        """在会议中发送聊天消息。

        参数:
            message (str): 要发送的消息。
        """
        async with self.animation("typing"):
            await self._meeting_provider.send_chat_message(message)

    async def get_chat_history(self) -> MeetingChatHistory:
        """获取会议的聊天历史。

        返回:
            MeetingChatHistory: 会议的聊天历史。
        """
        async with self.animation("reading"):
            return await self._meeting_provider.get_chat_history()

    async def get_participants(self) -> list[MeetingParticipant]:
        """获取会议参与者列表。

        返回:
            list[MeetingParticipant]: 会议中的参与者列表。
        """
        async with self.animation("reading"):
            return await self._meeting_provider.get_participants()

    async def get_video_snapshot(self) -> VideoSnapshot:
        """获取当前视频画面的快照。

        返回:
            VideoSnapshot: 当前视频快照。
        """
        return await self._video_reader.snapshot()

    async def share_screen(self, url: str) -> None:
        """开始在会议中共享屏幕。

        参数:
            url: 共享时展示的 URL。
        """
        async with self.animation("sharing"):
            await self._meeting_provider.share_screen(url)

    async def stop_sharing(self) -> None:
        """停止在会议中共享屏幕。"""
        await self._meeting_provider.stop_sharing()

    async def mute(self) -> None:
        """在会议中将自己静音。"""
        await self._meeting_provider.mute()
        self._is_muted = True

    async def unmute(self) -> None:
        """在会议中取消静音。"""
        await self._meeting_provider.unmute()
        self._is_muted = False

    async def set_animation(self, animation: ActionAnimation | None) -> None:
        """在会议提供方上设置动作动画。"""
        await self._meeting_provider.set_animation(animation)

    @asynccontextmanager
    async def animation(self, name: ActionAnimation) -> AsyncIterator[None]:
        """在代码块执行期间显示动作动画。"""
        await self.set_animation(name)
        try:
            yield
        finally:
            await self.set_animation(None)

    async def update_ui(self, update: UIUpdate) -> None:
        """更新会议提供方上的 UI。

        参数:
            update: 要应用的 UI 更新。
        """
        await self._meeting_provider.update_ui(update)
