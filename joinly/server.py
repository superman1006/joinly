"""joinly FastMCP 服务端入口。

暴露 MCP 工具（``join_meeting``、``speak_text``、``send_chat_message`` 等）与
资源（实时转写 ``transcript://live``、用量 ``usage://current``）。

每个 HTTP 客户端连接在 ``session_lifespan`` 中创建独立的 ``MeetingSession``；
可通过请求头 ``joinly-settings`` 传入 JSON 覆盖 ``Settings``。

运行方式::

    uv run joinly --port 8000          # 仅服务端
    uv run joinly --client <会议URL>   # 内置 Agent 客户端（见 main.py）
"""

import base64
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args

from fastmcp import Context, FastMCP
from mcp import types as mcp_types
from mcp.types import ImageContent
from pydantic import AnyUrl, BaseModel, Field, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from joinly.container import SessionContainer
from joinly.session import MeetingSession
from joinly.settings import Settings, get_settings, reset_settings, set_settings
from joinly.types import (
    MeetingChatHistory,
    MeetingParticipantList,
    SpeakerRole,
    SpeechInterruptedError,
    Transcript,
    UIUpdate,
    Usage,
)
from joinly.utils.usage import get_usage, reset_usage, set_usage

logger = logging.getLogger(__name__)

TRANSCRIPT_URL = AnyUrl("transcript://live")
SEGMENTS_URL = AnyUrl("transcript://live/segments")


class _UIUpdateNotification(BaseModel):
    method: Literal["notifications/joinly_ui_update"] = "notifications/joinly_ui_update"
    params: UIUpdate | None = None


def _patch_client_notifications(*types_: type) -> None:
    field = mcp_types.ClientNotification.model_fields["root"]
    current = get_args(field.annotation)
    field.annotation = Union[(*current, *types_)]  # type: ignore[assignment]
    mcp_types.ClientNotification.model_rebuild(force=True)


_patch_client_notifications(_UIUpdateNotification)


def _patch_experimental(server: FastMCP, extra: dict[str, dict[str, Any]]) -> None:
    """在 FastMCP 服务器上声明实验性能力。"""
    _orig = server._mcp_server.get_capabilities  # noqa: SLF001

    def _get_capabilities(opts: Any, exp: dict | None = None) -> Any:  # noqa: ANN401
        return _orig(opts, {**(exp or {}), **extra})

    server._mcp_server.get_capabilities = _get_capabilities  # type: ignore[assignment]  # noqa: SLF001


@dataclass
class SessionContext:
    """会议会话的上下文。"""

    meeting_session: MeetingSession


def _extract_settings() -> Settings:
    """从 HTTP 请求头解析配置。"""
    current = get_settings()
    try:
        from fastmcp.server.http import _current_http_request

        request = _current_http_request.get()
        header = request.headers.get("joinly-settings") if request is not None else None
    except RuntimeError:
        logger.exception("Failed to get HTTP headers")
        header = None

    if not header:
        return current

    try:
        base = current.model_copy(deep=True).model_dump()
        patch = Settings.model_validate(json.loads(header)).model_dump(
            exclude_unset=True
        )
        for k, v in patch.items():
            base[k] = (base.get(k, {}) | v) if isinstance(v, dict) else v
        settings = Settings.model_validate(base)
    except (json.JSONDecodeError, ValidationError):
        msg = "Invalid joinly-settings."
        logger.exception(msg)
        logger.warning("Continuing with current settings")
        return current

    return settings


@asynccontextmanager
async def session_lifespan(server: FastMCP) -> AsyncIterator[SessionContext]:
    """每个客户端连接创建并进入一次 MeetingSession。"""
    logger.info("Creating meeting session")
    settings = _extract_settings()
    settings_token = set_settings(settings)
    usage = Usage()
    usage_token = set_usage(usage)
    session_container = SessionContainer()
    meeting_session = await session_container.__aenter__()

    _remover: dict[AnyUrl, Callable[[], None]] = {}

    @server._mcp_server.subscribe_resource()  # noqa: SLF001
    async def _handle_subscribe_resource(url: AnyUrl) -> None:
        if url not in (TRANSCRIPT_URL, SEGMENTS_URL) or url in _remover:
            return
        logger.debug("Subscribing to resource: %s", url)
        session = server._mcp_server.request_context.session  # noqa: SLF001

        _event = "utterance" if url == TRANSCRIPT_URL else "segment"

        async def _push() -> None:
            logger.debug("Sending %s notification", _event)
            await session.send_resource_updated(url)

        _remover[url] = meeting_session.subscribe(_event, _push)

    @server._mcp_server.unsubscribe_resource()  # noqa: SLF001
    async def _handle_unsubscribe_resource(url: AnyUrl) -> None:
        if url in _remover:
            logger.debug("Unsubscribing from resource: %s", url)
            _remover[url]()
            _remover.pop(url)

    async def _handle_ui_update(notification: _UIUpdateNotification) -> None:
        if not notification.params:
            return
        logger.debug("UI update: %s", notification.params.content)
        await meeting_session.update_ui(notification.params)

    server._mcp_server.notification_handlers[_UIUpdateNotification] = _handle_ui_update  # noqa: SLF001

    try:
        yield SessionContext(meeting_session=meeting_session)
    finally:
        for _rem in _remover.values():
            _rem()

        # 确保资源正确清理
        from anyio import CancelScope

        with CancelScope(shield=True):
            await session_container.__aexit__()

        reset_settings(settings_token)
        reset_usage(usage_token)


mcp = FastMCP("joinly", lifespan=session_lifespan)
_patch_experimental(mcp, {"joinly_ui_update": {}})


@mcp.resource(
    str(TRANSCRIPT_URL),
    description="会议参与者话语的实时转写全文。",
    mime_type="application/json",
)
async def get_transcript(ctx: Context) -> Transcript:
    """获取会议的实时转写全文。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    return ms.transcript.with_role(SpeakerRole.participant)


@mcp.resource(
    str(SEGMENTS_URL),
    description="实时转写片段。",
    mime_type="application/json",
)
async def get_transcript_segments(ctx: Context) -> Transcript:
    """获取会议的实时转写片段。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    return ms.transcript


@mcp.resource(
    "usage://current",
    description="各服务的当前用量统计",
    mime_type="application/json",
)
async def get_usage_report(_ctx: Context) -> Usage:
    """获取当前用量统计。"""
    return get_usage()


@mcp.tool(
    "join_meeting",
    description="使用给定 URL 与参与者名称加入会议。",
)
async def join_meeting(
    ctx: Context,
    meeting_url: Annotated[
        str | None, Field(default=None, description="加入在线会议的 URL")
    ],
    participant_name: Annotated[
        str | None,
        Field(default=None, description="加入会议时使用的参与者名称"),
    ],
    passcode: Annotated[
        str | None,
        Field(
            default=None,
            description="会议密码或通行码（若需要）",
        ),
    ] = None,
) -> str:
    """使用给定 URL 与参与者名称加入会议。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.join_meeting(meeting_url, participant_name, passcode)
    return "Joined meeting."


@mcp.tool(
    "leave_meeting",
    description="离开当前会议。",
)
async def leave_meeting(
    ctx: Context,
) -> str:
    """离开当前会议。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.leave_meeting()
    return "Left the meeting."


@mcp.tool(
    "speak_text",
    description="在会议中朗读给定文本。",
)
async def speak_text(
    ctx: Context,
    text: Annotated[str, Field(description="要朗读的文本")],
) -> str:
    """使用 TTS 在会议中朗读给定文本。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    try:
        await ms.speak_text(text)
    except SpeechInterruptedError as e:
        return str(e)
    return "Finished speaking."


@mcp.tool(
    "send_chat_message",
    description="在会议聊天中发送消息。",
)
async def send_chat_message(
    ctx: Context,
    message: Annotated[str, Field(description="要发送的消息")],
) -> str:
    """在会议中发送聊天消息。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.send_chat_message(message)
    return "Sent message."


@mcp.tool(
    "get_chat_history",
    description="获取会议内聊天的历史记录。",
)
async def get_chat_history(
    ctx: Context,
) -> MeetingChatHistory:
    """获取会议的聊天历史。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    return await ms.get_chat_history()


@mcp.tool(
    "get_transcript",
    description=(
        "获取会议转写。默认返回完整转写。"
        "若要切片，将 mode 设为 'first' 或 'latest'，并提供正数的分钟数。"
    ),
)
async def get_transcript_tool(
    ctx: Context,
    mode: Annotated[
        Literal["full", "first", "latest"],
        Field(
            default="full",
            description="获取模式：'full' 为完整转写，"
            "'first' 为前 N 分钟，'latest' 为最近 N 分钟。",
        ),
    ] = "full",
    minutes: Annotated[
        int,
        Field(
            default=0,
            description="切片用的分钟数。仅在 mode 为 'first' 或 'latest' 时使用。",
        ),
    ] = 0,
) -> Transcript:
    """获取会议转写。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    if mode == "first":
        return ms.transcript.before(minutes * 60).compact()
    if mode == "latest":
        return ms.transcript.after(ms.meeting_seconds - minutes * 60).compact()
    return ms.transcript.compact()


@mcp.tool(
    "get_participants",
    description="获取会议中的参与者列表。",
)
async def get_participants(
    ctx: Context,
) -> MeetingParticipantList:
    """获取会议中的参与者列表。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    return MeetingParticipantList(await ms.get_participants())


@mcp.tool(
    "get_video_snapshot",
    description=("获取当前视频画面快照，包括会议中的参与者摄像头与屏幕共享。"),
)
async def get_video_snapshot(ctx: Context) -> ImageContent:
    """获取当前视频画面快照。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    snapshot = await ms.get_video_snapshot()
    return ImageContent(
        type="image",
        data=base64.b64encode(snapshot.data).decode(),
        mimeType=snapshot.media_type,
    )


@mcp.tool(
    "share_screen",
    description=(
        "开始在会议中共享屏幕。提供要在共享时展示的 URL，"
        "参与者将看到该 URL 对应的内容。"
    ),
)
async def share_screen(
    ctx: Context,
    url: Annotated[
        str,
        Field(description="共享屏幕时要打开并展示的 URL"),
    ],
) -> str:
    """开始在会议中共享屏幕。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.share_screen(url)
    return "Started sharing screen."


@mcp.tool(
    "stop_sharing",
    description="停止在会议中共享屏幕。",
)
async def stop_sharing(
    ctx: Context,
) -> str:
    """停止在会议中共享屏幕。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.stop_sharing()
    return "Stopped sharing screen."


@mcp.tool(
    "mute_yourself",
    description="在会议中将自己静音。",
)
async def mute_yourself(
    ctx: Context,
) -> str:
    """在会议中将自己静音。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.mute()
    return "Muted yourself."


@mcp.tool(
    "unmute_yourself",
    description="在会议中取消静音。",
)
async def unmute_yourself(
    ctx: Context,
) -> str:
    """在会议中取消静音。"""
    ms: MeetingSession = ctx.request_context.lifespan_context.meeting_session
    await ms.unmute()
    return "Unmuted yourself."


# 百度 AppBuilder AI 搜索的 SSE MCP 端点
# 工具名为 "AIsearch"（区分大小写，非 ai_search/AI_search）
_BAIDU_SEARCH_SSE = "http://appbuilder.baidu.com/v2/ai_search/mcp/sse"


@mcp.tool(
    "web_search",
    description=(
        "使用百度 AI 搜索实时查询互联网信息。"
        "适用于需要最新资讯、查找事实、了解未知概念等场景。"
        "返回搜索结果摘要文本。"
    ),
)
async def web_search(
    query: Annotated[str, Field(description="搜索关键词或问题")],
) -> str:
    """调用百度 AI 搜索 MCP（SSE 协议）获取实时网络信息。

    需要在环境变量 ``BAIDU_SEARCH_API_KEY`` 中配置百度 AppBuilder API Key。
    使用 fastmcp.Client 而非手动 POST，以正确处理 SSE 协议握手与流式事件。
    """
    from fastmcp import Client

    api_key = os.environ.get("BAIDU_SEARCH_API_KEY", "")
    if not api_key:
        return "未配置百度搜索 API Key（环境变量 BAIDU_SEARCH_API_KEY）。"

    url = f"{_BAIDU_SEARCH_SSE}?api_key={api_key}"
    try:
        async with Client(url) as client:
            result = await client.call_tool("AIsearch", {"query": query})
        texts = [item.text for item in result.content if hasattr(item, "text") and item.text]
        return "\n".join(texts) if texts else "未获取到搜索结果。"
    except Exception as e:
        logger.exception("百度搜索调用异常")
        return f"百度搜索出错: {e}"


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_req: Request) -> JSONResponse:
    """健康检查端点。"""
    return JSONResponse({"status": "healthy"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
