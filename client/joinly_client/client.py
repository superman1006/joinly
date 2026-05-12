import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from typing import Any, Self

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp import ClientSession, McpError, ResourceUpdatedNotification, ServerNotification
from mcp.types import Tool
from pydantic import AnyUrl, BaseModel

from joinly_client.types import (
    MeetingChatHistory,
    MeetingParticipantList,
    SpeakerRole,
    ToolExecutor,
    Transcript,
    TranscriptSegment,
    UIAnimation,
    UIAnimationContent,
    UIUpdate,
    Usage,
    VideoSnapshot,
)
from joinly_client.utils import is_async_context, name_in_transcript

logger = logging.getLogger(__name__)


class _UIUpdateNotification(BaseModel):
    method: str = "notifications/joinly_ui_update"
    params: UIUpdate | None = None


TRANSCRIPT_URL = AnyUrl("transcript://live")
SEGMENTS_URL = AnyUrl("transcript://live/segments")
USAGE_URL = AnyUrl("usage://current")


class JoinlyClient:
    """与 joinly 服务端交互的客户端。"""

    def __init__(
        self,
        url: str | FastMCP,
        *,
        name: str | None = None,
        name_trigger: bool = False,
        settings: dict[str, Any] | None = None,
    ) -> None:
        """使用服务端 URL 初始化 JoinlyClient。

        参数:
            url (str | FastMCP): joinly 服务端 URL，或 FastMCP 实例。
            name (str | None): 参与者名称，默认 "joinly"。
            name_trigger (bool): 是否仅在转写中提到名称时才触发话语回调。
            settings (dict[str, Any]): 客户端附加配置。
        """
        self.url = url
        self.settings = settings or {}
        self.name: str = name or self.settings.get("name", "joinly")
        self.name_trigger = name_trigger
        self.settings["name"] = self.name

        self.joined: bool = False
        self._client: Client | None = None
        self._stack = AsyncExitStack()
        self._utterance_callbacks: set[
            Callable[[list[TranscriptSegment]], Coroutine[None, None, None]]
        ] = set()
        self._last_utterance: float = 0.0
        self._segment_callbacks: set[
            Callable[[list[TranscriptSegment]], Coroutine[None, None, None]]
        ] = set()
        self._last_segment: float = 0.0
        self._tasks: set[asyncio.Task] = set()

    @property
    def client(self) -> Client:
        """获取当前客户端实例。

        返回:
            Client: 当前客户端实例。

        引发:
            RuntimeError: 若客户端尚未连接。
        """
        if self._client is None:
            msg = "Client is not connected"
            raise RuntimeError(msg)
        return self._client

    @property
    def session(self) -> ClientSession:
        """获取当前会话实例。

        返回:
            ClientSession: 当前 MCP 会话实例。

        引发:
            RuntimeError: 若客户端尚未连接。
        """
        return self.client.session

    def add_utterance_callback(
        self, callback: Callable[[list[TranscriptSegment]], Coroutine[None, None, None]]
    ) -> Callable[[], None]:
        """添加在话语（utterance）事件上调用的回调。

        参数:
            callback (Callable[[list[TranscriptSegment]], Coroutine[None, None, None]]):
                收到新转写片段时调用的回调。

        返回:
            Callable[[], None]: 用于移除该回调的函数。
        """
        if (
            self._client is not None
            and not self._utterance_callbacks
            and is_async_context()
        ):
            # 更新最近话语并订阅
            async def _subscribe() -> None:
                await self._utterance_update()
                self._utterance_callbacks.add(callback)
                await self.client.session.subscribe_resource(TRANSCRIPT_URL)

            self._track_task(asyncio.create_task(_subscribe()))
        else:
            self._utterance_callbacks.add(callback)

        def remove_callback() -> None:
            """从话语回调列表中移除该回调。"""
            self._utterance_callbacks.discard(callback)
            if (
                self._client is not None
                and not self._utterance_callbacks
                and is_async_context()
            ):
                self._track_task(
                    asyncio.create_task(
                        self._client.session.unsubscribe_resource(TRANSCRIPT_URL)
                    )
                )

        return remove_callback

    def add_segment_callback(
        self, callback: Callable[[list[TranscriptSegment]], Coroutine[None, None, None]]
    ) -> Callable[[], None]:
        """添加在片段事件上调用的回调。

        参数:
            callback (Callable[[list[TranscriptSegment]], Coroutine[None, None, None]]):
                收到新转写片段时调用的回调。

        返回:
            Callable[[], None]: 用于移除该回调的函数。
        """
        if (
            self._client is not None
            and not self._segment_callbacks
            and is_async_context()
        ):
            # 更新最近片段并订阅
            async def _subscribe() -> None:
                await self._segment_update()
                self._segment_callbacks.add(callback)
                await self.client.session.subscribe_resource(SEGMENTS_URL)

            self._track_task(asyncio.create_task(_subscribe()))
        else:
            self._segment_callbacks.add(callback)

        def remove_callback() -> None:
            """从片段回调列表中移除该回调。"""
            self._segment_callbacks.discard(callback)
            if (
                self._client is not None
                and not self._segment_callbacks
                and is_async_context()
            ):
                self._track_task(
                    asyncio.create_task(
                        self._client.session.unsubscribe_resource(SEGMENTS_URL)
                    )
                )

        return remove_callback

    async def __aenter__(self) -> Self:
        """连接到 joinly 服务端。"""
        await self._connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """断开与 joinly 服务端的连接。"""
        self._utterance_callbacks.clear()
        self._segment_callbacks.clear()
        for task in list(self._tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._stack.aclose()
        self._client = None

    async def _connect(self) -> None:  # noqa: C901
        """连接到 joinly 服务端。"""
        if self._client is not None:
            msg = "Already connected to the joinly server"
            raise RuntimeError(msg)

        async def _message_handler(message) -> None:  # noqa: ANN001
            if isinstance(message, ServerNotification) and isinstance(
                message.root, ResourceUpdatedNotification
            ):
                if message.root.params.uri == TRANSCRIPT_URL:
                    self._track_task(asyncio.create_task(self._utterance_update()))
                elif message.root.params.uri == SEGMENTS_URL:
                    self._track_task(asyncio.create_task(self._segment_update()))

        if isinstance(self.url, str):
            transport = StreamableHttpTransport(
                url=self.url,
                headers={"joinly-settings": json.dumps(self.settings)},
            )
            logger.info("Connecting to joinly server at %s", self.url)
        else:
            transport = self.url

        self._client = Client(transport=transport, message_handler=_message_handler)
        try:
            await self._stack.enter_async_context(self._client)
        except Exception:
            logger.exception("Failed to connect to joinly server")
            await self._stack.aclose()
            raise
        else:
            logger.debug("Connected to joinly server")

        if self._utterance_callbacks:
            await self._client.session.subscribe_resource(TRANSCRIPT_URL)
        if self._segment_callbacks:
            await self._client.session.subscribe_resource(SEGMENTS_URL)

    def _track_task(self, task: asyncio.Task) -> None:
        """跟踪任务以便在退出时清理。

        参数:
            task (asyncio.Task): 要跟踪的任务。
        """
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(
            lambda t: t.exception()
            and logger.error("Task %s failed with exception: %s", t, t.exception())
        )

    async def _utterance_update(self) -> None:
        """用新片段更新话语回调。"""
        if not self.joined:
            return

        resource = await self.client.read_resource(TRANSCRIPT_URL)
        transcript = Transcript.model_validate_json(resource[0].text)  # type: ignore[attr-defined]
        new_transcript = transcript.with_role(SpeakerRole.participant).after(
            self._last_utterance
        )
        if new_transcript.segments and (
            not self.name_trigger or name_in_transcript(new_transcript, self.name)
        ):
            self._last_utterance = new_transcript.segments[-1].start
            for callback in self._utterance_callbacks:
                self._track_task(
                    asyncio.create_task(callback(new_transcript.compact().segments))
                )

    async def _segment_update(self) -> None:
        """用新片段更新片段回调。"""
        if not self.joined:
            return

        resource = await self.client.read_resource(SEGMENTS_URL)
        transcript = Transcript.model_validate_json(resource[0].text)  # type: ignore[attr-defined]
        new_transcript = transcript.after(self._last_segment)
        if new_transcript.segments:
            self._last_segment = new_transcript.segments[-1].start
            for callback in self._segment_callbacks:
                self._track_task(asyncio.create_task(callback(new_transcript.segments)))

    async def list_tools(self) -> list[Tool]:
        """列出 joinly 服务端上的可用工具。

        返回:
            list[Tool]: 可用工具列表。
        """
        return await self.client.list_tools()

    async def join_meeting(
        self,
        meeting_url: str | None,
        passcode: str | None = None,
        participant_name: str | None = None,
    ) -> None:
        """通过 joinly 服务端加入会议。

        参数:
            meeting_url (str | None): 要加入的会议 URL。
            passcode (str | None): 会议通行码（若需要）。
            participant_name (str | None): 参与者显示名称。
        """
        if participant_name is not None:
            self.name = participant_name
        logger.info("Joining meeting at %s", meeting_url)
        await self.client.call_tool(
            "join_meeting",
            arguments={
                "meeting_url": meeting_url,
                "passcode": passcode,
                "participant_name": self.name,
            },
        )
        logger.info("Joined meeting successfully")
        self.joined = True
        self._last_utterance = 0.0
        self._last_segment = 0.0

    async def leave_meeting(self) -> None:
        """离开当前会议。"""
        if not self.joined:
            msg = "Not joined to a meeting"
            raise RuntimeError(msg)

        await self.client.call_tool("leave_meeting")
        self.joined = False
        self._last_utterance = 0.0
        self._last_segment = 0.0

    async def get_transcript(self) -> Transcript:
        """从服务端获取完整转写。

        返回:
            Transcript: 当前转写。
        """
        if not self.joined:
            return Transcript(segments=[])

        result = await self.client.call_tool("get_transcript")
        return Transcript.model_validate_json(result.content[0].text)  # type: ignore[attr-defined]

    async def get_chat_history(self) -> MeetingChatHistory:
        """获取会议的聊天历史。

        返回:
            MeetingChatHistory: 会议的聊天历史。
        """
        if not self.joined:
            return MeetingChatHistory(messages=[])

        result = await self.client.call_tool("get_chat_history")
        return MeetingChatHistory.model_validate_json(result.content[0].text)  # type: ignore[attr-defined]

    async def get_participants(self) -> MeetingParticipantList:
        """获取会议参与者列表。

        返回:
            MeetingParticipantList: 参与者列表。
        """
        if not self.joined:
            return MeetingParticipantList()

        result = await self.client.call_tool("get_participants")
        return MeetingParticipantList.model_validate_json(result.content[0].text)  # type: ignore[attr-defined]

    async def get_usage(self) -> Usage:
        """从服务端获取当前用量统计。

        返回:
            Usage: 当前用量统计。
        """
        try:
            result = await self.client.read_resource(USAGE_URL)
        except McpError:
            logger.warning("Failed to get usage statistics")
            return Usage()
        else:
            return Usage.model_validate_json(result[0].text)  # type: ignore[attr-defined]

    async def speak_text(self, text: str) -> None:
        """通过 joinly 服务端朗读给定文本。

        参数:
            text (str): 要朗读的文本。
        """
        if not self.joined:
            msg = "Not joined to a meeting"
            raise RuntimeError(msg)

        await self.client.call_tool(
            "speak_text",
            arguments={"text": text},
        )

    async def send_chat_message(self, message: str) -> None:
        """在会议中发送聊天消息。

        参数:
            message (str): 要发送的聊天消息。
        """
        if not self.joined:
            msg = "Not joined to a meeting"
            raise RuntimeError(msg)

        await self.client.call_tool(
            "send_chat_message",
            arguments={"message": message},
        )

    async def get_video_snapshot(self) -> VideoSnapshot:
        """获取当前视频画面的快照。

        返回:
            VideoSnapshot: 包含原始图像数据与 MIME 类型的快照。
        """
        if not self.joined:
            msg = "Not joined to a meeting"
            raise RuntimeError(msg)

        result = await self.client.call_tool("get_video_snapshot")
        content = result.content[0]
        return VideoSnapshot(
            data=base64.b64decode(content.data),  # type: ignore[union-attr]
            media_type=content.mimeType,  # type: ignore[union-attr]
        )

    async def share_screen(self, url: str) -> None:
        """开始在会议中共享屏幕。

        参数:
            url (str): 共享时要展示的 URL。
        """
        if not self.joined:
            msg = "Not joined to a meeting"
            raise RuntimeError(msg)

        await self.client.call_tool(
            "share_screen",
            arguments={"url": url},
        )

    async def stop_sharing(self) -> None:
        """停止在会议中共享屏幕。"""
        if not self.joined:
            msg = "Not joined to a meeting"
            raise RuntimeError(msg)

        await self.client.call_tool("stop_sharing")

    async def mute(self) -> None:
        """将会议中的参与者静音。"""
        if not self.joined:
            msg = "Not joined to a meeting"
            raise RuntimeError(msg)

        await self.client.call_tool("mute_yourself")

    async def unmute(self) -> None:
        """取消会议中参与者的静音。"""
        if not self.joined:
            msg = "Not joined to a meeting"
            raise RuntimeError(msg)

        await self.client.call_tool("unmute_yourself")

    @property
    def supports_ui_update(self) -> bool:
        """检查服务端是否支持 joinly_ui_update 通知。"""
        caps = self.client.initialize_result.capabilities
        return bool(caps.experimental and "joinly_ui_update" in caps.experimental)

    async def on_agent_status(self, status: str | None) -> None:
        """将智能体状态映射为 UI 动画。"""
        _map: dict[str, UIAnimation] = {"llm_call": "thinking", "tool_call": "busy"}
        await self.set_ui_animation(_map.get(status or ""))

    async def set_ui_animation(self, animation: UIAnimation | None) -> None:
        """按名称设置 UI 动画；传入 None 清除叠加层。"""
        await self.send_ui_update(
            UIUpdate(content=UIAnimationContent(animation=animation))
        )

    async def send_ui_update(self, update: UIUpdate) -> None:
        """向服务端发送 UI 更新通知。

        若服务端未声明 joinly_ui_update 实验能力，则不做任何事。

        参数:
            update: 要发送的 UI 更新。
        """
        if not self.supports_ui_update:
            return
        await self.session.send_notification(
            _UIUpdateNotification(params=update)  # type: ignore[arg-type]
        )

    def create_agent(
        self,
        llm: Any,  # noqa: ANN401
        tools: list[Any],
        tool_executor: ToolExecutor,
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """创建绑定到本客户端的 ConversationalToolAgent。

        会连接状态回调并注册智能体的话语处理逻辑。

        参数:
            llm: 使用的大语言模型。
            tools: 智能体可用的工具定义。
            tool_executor: 执行工具调用的可调用对象。
            **kwargs: 透传给 ``ConversationalToolAgent`` 的其余参数。
        """
        from joinly_client.agent import ConversationalToolAgent

        agent = ConversationalToolAgent(
            llm,
            tools,
            tool_executor,
            on_status=self.on_agent_status,
            **kwargs,
        )
        self.add_utterance_callback(agent.on_utterance)
        return agent
