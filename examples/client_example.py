# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "fastmcp",
#     "langchain",
#     "langchain-anthropic",
#     "langchain-mcp-adapters",
#     "langchain-ollama",
#     "langchain-openai",
#     "langgraph",
#     "py-dotenv",
#     "rich",
# ]
# ///

import asyncio
import contextlib
import datetime
import json
import logging
import os
import re

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import ToolNode, create_react_agent
from mcp import ResourceUpdatedNotification, ServerNotification
from pydantic import AnyUrl, BaseModel

logger = logging.getLogger(__name__)


class TranscriptSegment(BaseModel):
    """转写中的一个片段。"""

    text: str
    start: float
    end: float
    speaker: str | None = None


class Transcript(BaseModel):
    """包含多个片段的转写。"""

    segments: list[TranscriptSegment]


def transcript_to_messages(transcript: Transcript) -> list[HumanMessage]:
    """将转写转换为 HumanMessage 列表。"""

    def _normalize_speaker(speaker: str | None) -> str:
        if speaker is None:
            return "Unknown"
        speaker = re.sub(r"\s+", "_", speaker.strip())
        return re.sub(r"[<>\|\\\/]+", "", speaker)

    return [
        HumanMessage(
            content=s.text,
            name=_normalize_speaker(s.speaker),
        )
        for s in transcript.segments
    ]


def transcript_after(transcript: Transcript, after: float) -> Transcript:
    """返回仅包含指定时间之后片段的新转写。"""
    segments = [s for s in transcript.segments if s.start > after]
    return Transcript(segments=segments)


def log_chunk(chunk) -> None:  # noqa: ANN001
    """记录来自 langgraph 的更新块。"""
    if "agent" in chunk:
        for m in chunk["agent"]["messages"]:
            for t in m.tool_calls or []:
                args_str = ", ".join(
                    f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}"
                    for k, v in t.get("args", {}).items()
                )
                logger.info("%s: %s", t["name"], args_str)
    if "tools" in chunk:
        for m in chunk["tools"]["messages"]:
            logger.info("%s: %s", m.name, m.content)


async def run(  # noqa: C901
    mcp_url: str,
    meeting_url: str,
    model_name: str,
    model_provider: str | None = None,
    config: dict | None = None,
) -> None:
    """用于会议的简单对话式智能体。

    参数:
        mcp_url: MCP 服务器 URL。
        meeting_url: 要加入的会议 URL。
        model_name: 智能体使用的大语言模型名称。
        model_provider: 大语言模型提供方。
        config: 附加 MCP 服务器的可选配置。
    """
    transcript_url = AnyUrl("transcript://live")
    transcript_event = asyncio.Event()

    async def _message_handler(message) -> None:  # noqa: ANN001
        if (
            isinstance(message, ServerNotification)
            and isinstance(message.root, ResourceUpdatedNotification)
            and message.root.params.uri == transcript_url
        ):
            transcript_event.set()

    llm = init_chat_model(model_name, model_provider=model_provider)

    prompt = (
        f"Today is {datetime.datetime.now(tz=datetime.UTC).strftime('%d.%m.%Y')}. "
        "You are joinly, a professional and knowledgeable meeting assistant. "
        "Provide concise, valuable contributions in the meeting. "
        "You are only with one other participant in the meeting, therefore "
        "respond to all messages and questions. "
        "When you are greeted, respond politely in spoken language. "
        "Give information, answer questions, and fullfill tasks as needed. "
        "You receive real-time transcripts from the ongoing meeting. "
        "Respond interactively and use available tools to assist participants. "
        "Always finish your response with the 'finish' tool. "
        "Never directly use the 'finish' tool, always respond first and then use it. "
        "If interrupted mid-response, use 'finish'."
    )

    # 可选：为 joinly 设置配置（需要 v0.3.2）
    settings = {
        # "name": "joinly",  # noqa: ERA001
        # "language": "en",  # noqa: ERA001
        # "tts": "elevenlabs",  # noqa: ERA001
    }
    transport = StreamableHttpTransport(
        url=mcp_url, headers={"joinly-settings": json.dumps(settings)}
    )

    # 使用单独的 joinly 客户端：fastmcp 在代理服务器模式下尚不支持通知（v2.7.0）
    joinly_client = Client(transport, message_handler=_message_handler)
    client = Client(config) if config and config.get("mcpServers") else None

    mcp_servers = list(config.get("mcpServers", {}).keys()) if config else None
    logger.info(
        "Connecting to joinly MCP server at %s and following other MCP servers: %s",
        mcp_url,
        mcp_servers,
    )
    async with joinly_client, client or contextlib.nullcontext():
        if joinly_client.is_connected():
            logger.info("Connected to joinly MCP server")
        else:
            logger.error("Failed to connect to joinly MCP server at %s", mcp_url)
        if client and not client.is_connected():
            logger.error("Failed to connect to additional MCP servers: %s", mcp_servers)

        await joinly_client.session.subscribe_resource(transcript_url)

        @tool(return_direct=True)
        def finish() -> str:
            """结束本轮对话的工具。"""
            return "Finished."

        # 从 joinly 及其他 MCP 服务器加载工具
        tools = await load_mcp_tools(joinly_client.session)
        if client:
            tools.extend(await load_mcp_tools(client.session))
        tools.append(finish)

        tool_node = ToolNode(tools, handle_tool_errors=lambda e: e)
        llm_binded = llm.bind_tools(tools, tool_choice="any")

        memory = MemorySaver()
        agent = create_react_agent(
            llm_binded, tool_node, prompt=prompt, checkpointer=memory
        )
        last_time = -1.0

        logger.info("Joining meeting at %s", meeting_url)
        await joinly_client.call_tool("join_meeting", {"meeting_url": meeting_url})
        logger.info("Joined meeting successfully")

        while True:
            await transcript_event.wait()
            transcript_full = Transcript.model_validate_json(
                (await joinly_client.read_resource(transcript_url))[0].text  # type: ignore[attr-defined]
            )
            transcript = transcript_after(transcript_full, after=last_time)
            transcript_event.clear()
            if not transcript.segments:
                logger.warning("No new segments in the transcript after update")
                continue

            last_time = transcript.segments[-1].start
            for segment in transcript.segments:
                logger.info(
                    '%s: "%s"',
                    segment.speaker if segment.speaker else "User",
                    segment.text,
                )

            try:
                async for chunk in agent.astream(
                    {"messages": transcript_to_messages(transcript)},
                    config={"configurable": {"thread_id": "1"}},
                    stream_mode="updates",
                ):
                    log_chunk(chunk)
            except Exception:
                logger.exception("Error during agent invocation")


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    from dotenv import load_dotenv
    from rich.logging import RichHandler

    load_dotenv()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    logger.setLevel(logging.INFO)

    parser = argparse.ArgumentParser(
        description=(
            "使用 joinly.ai 运行会议对话式智能体；"
            "可选连接多个 MCP 服务器。"
        )
    )
    parser.add_argument("meeting_url", help="要加入的会议 URL")
    parser.add_argument(
        "--mcp-url",
        dest="mcp_url",
        default="http://localhost:8000/mcp/",
        help="joinly MCP 服务器 URL",
    )
    parser.add_argument(
        "--model-name",
        dest="model_name",
        default=os.getenv("JOINLY_MODEL_NAME", "gpt-4o"),
        help="智能体使用的大语言模型名称",
    )
    parser.add_argument(
        "--model-provider",
        dest="model_provider",
        default=os.getenv("JOINLY_MODEL_PROVIDER"),
        help="大语言模型提供方",
    )
    parser.add_argument(
        "--config",
        dest="config",
        type=str,
        default=None,
        help=(
            "附加 MCP 服务器的 JSON 配置文件路径。"
            "示例："
            '\'{"mcpServers": {"remote": {"url": "https://example.com/mcp"}}}\'。'
            "详见 https://gofastmcp.com/clients/client。"
        ),
    )
    args = parser.parse_args()
    config = None
    if args.config:
        try:
            with Path(args.config).open("r") as f:
                config = json.load(f)
        except Exception:
            logger.exception("Failed to load configuration file")
            args.config = None

    asyncio.run(
        run(
            mcp_url=args.mcp_url,
            meeting_url=args.meeting_url,
            model_name=args.model_name,
            model_provider=args.model_provider,
            config=config,
        )
    )
