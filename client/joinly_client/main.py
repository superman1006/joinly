import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv
from fastmcp import Client, FastMCP

from joinly_client.client import JoinlyClient
from joinly_client.types import McpClientConfig, TranscriptSegment
from joinly_client.utils import get_llm, get_prompt, load_tools

logger = logging.getLogger(__name__)


def _parse_kv(
    _ctx: click.Context, _param: click.Parameter, value: tuple[str]
) -> dict[str, object] | None:
    """将 (--foo-arg key=value) 形式的重复元组解析为字典。"""
    out: dict[str, object] = {}
    for item in value:
        try:
            k, v = item.split("=", 1)
        except ValueError as exc:
            msg = f"{item!r} is not of the form key=value"
            raise click.BadParameter(msg) from exc

        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out or None


@click.command()
@click.option(
    "--joinly-url",
    type=str,
    help="要连接的 joinly 服务端 URL。",
    default="http://localhost:8000/mcp/",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_URL",
)
@click.option(
    "-n",
    "--name",
    type=str,
    help="会议中的参与者显示名称。",
    default="joinly",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_NAME",
)
@click.option(
    "--llm-provider",
    "--model-provider",
    type=str,
    help="客户端使用的大语言模型提供方。",
    default="openai",
    show_default=True,
    show_envvar=True,
    envvar=["JOINLY_LLM_PROVIDER", "JOINLY_MODEL_PROVIDER"],
)
@click.option(
    "--llm-model",
    "--model-name",
    type=str,
    help="客户端使用的大语言模型名称。",
    default="gpt-4o",
    show_default=True,
    show_envvar=True,
    envvar=["JOINLY_LLM_MODEL", "JOINLY_MODEL_NAME"],
)
@click.option(
    "--env-file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="用于加载环境变量的 .env 文件路径。",
    default=None,
    show_default=True,
    is_eager=True,
    expose_value=False,
    callback=lambda _ctx, _param, value: load_dotenv(value),
)
@click.option(
    "--prompt",
    type=str,
    help="模型的系统提示词；未提供则使用默认系统提示词。",
    default=None,
    envvar="JOINLY_PROMPT",
)
@click.option(
    "--prompt-file",
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="包含系统提示词的文本文件路径。",
    default=None,
    show_default=True,
    envvar="JOINLY_PROMPT_FILE",
)
@click.option(
    "--prompt-style",
    type=click.Choice(["dyadic", "mpc"], case_sensitive=False),
    help="未提供自定义提示词时使用的默认类型："
    "'dyadic' 适用于一对一会议，'mpc' 适用于多人会议。",
    default="mpc",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_PROMPT_STYLE",
)
@click.option(
    "--mcp-config",
    type=str,
    help="附加 MCP 服务器的 JSON 配置文件路径。"
    "文件内容示例："
    '\'{"mcpServers": {"remote": {"url": "https://example.com/mcp"}}}\'。'
    "详见 https://gofastmcp.com/clients/client。",
    default=None,
)
@click.option(
    "--name-trigger",
    is_flag=True,
    help="仅在转写中出现本参与者名称时再触发智能体。",
)
@click.option(
    "--language",
    "--lang",
    type=str,
    help="转写与语音合成所使用的语言。",
    default=None,
    show_envvar=True,
    envvar="JOINLY_LANGUAGE",
)
@click.option(
    "--vad",
    type=str,
    help='要使用的语音活动检测服务，可选："silero"、"webrtc"。',
    default=None,
    show_envvar=True,
    envvar="JOINLY_VAD",
)
@click.option(
    "--stt",
    type=str,
    help='要使用的语音转文字服务，可选："whisper"（本地）、"deepgram"。',
    default=None,
    show_envvar=True,
    envvar="JOINLY_STT",
)
@click.option(
    "--tts",
    type=str,
    help='要使用的文本转语音服务，可选："kokoro"（本地）、'
    '"elevenlabs"、"deepgram"。',
    default=None,
    show_envvar=True,
    envvar="JOINLY_TTS",
)
@click.option(
    "--vad-arg",
    "vad_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="以 key=value 形式传入 VAD 服务参数，可多次指定。",
)
@click.option(
    "--stt-arg",
    "stt_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="以 key=value 形式传入 STT 服务参数，可多次指定。",
)
@click.option(
    "--tts-arg",
    "tts_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="以 key=value 形式传入 TTS 服务参数，可多次指定。",
)
@click.option(
    "--transcription-controller-arg",
    "transcription_controller_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="以 key=value 形式传入转写控制器参数，可多次指定。",
)
@click.option(
    "--speech-controller-arg",
    "speech_controller_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="以 key=value 形式传入语音输出控制器参数，可多次指定。",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="提高日志详细程度（可重复使用）。",
    default=1,
)
@click.option(
    "-q", "--quiet", is_flag=True, help="仅保留错误与严重级别的日志输出。"
)
@click.argument(
    "meeting-url",
    type=str,
    required=True,
)
def cli(  # noqa: PLR0913
    *,
    joinly_url: str,
    name: str,
    llm_provider: str,
    llm_model: str,
    prompt: str | None,
    prompt_file: str | None,
    prompt_style: str,
    name_trigger: bool,
    mcp_config: str | None,
    meeting_url: str,
    verbose: int,
    quiet: bool,
    **settings: Any,  # noqa: ANN401
) -> None:
    """运行 joinly 客户端。"""
    from rich.logging import RichHandler

    log_level = logging.WARNING
    if quiet:
        log_level = logging.ERROR
    elif verbose == 1:
        log_level = logging.INFO
    elif verbose == 2:  # noqa: PLR2004
        log_level = logging.DEBUG

    logging.basicConfig(
        level=logging.WARNING if not quiet else logging.ERROR,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )
    logging.getLogger("joinly_client").setLevel(log_level)

    if prompt_file and not prompt:
        try:
            with Path(prompt_file).open("r") as f:
                prompt = f.read().strip()
        except Exception:
            logger.exception("Failed to load prompt file")
            prompt = None

    mcp_config_dict: dict[str, Any] | None = None
    if mcp_config:
        try:
            with Path(mcp_config).open("r") as f:
                mcp_config_dict = json.load(f)
        except Exception:
            logger.exception("Failed to load MCP configuration file")
            mcp_config_dict = None

    try:
        asyncio.run(
            run(
                joinly_url=joinly_url,
                meeting_url=meeting_url,
                llm_provider=llm_provider,
                llm_model=llm_model,
                prompt=prompt,
                prompt_style=prompt_style,
                name=name,
                name_trigger=name_trigger,
                mcp_config=mcp_config_dict,
                settings={k: v for k, v in settings.items() if v is not None},
            )
        )
    except KeyboardInterrupt:
        logger.info("Exiting due to keyboard interrupt.")


async def run(  # noqa: PLR0913
    joinly_url: str | FastMCP,
    meeting_url: str,
    llm_provider: str,
    llm_model: str,
    *,
    prompt: str | None = None,
    prompt_style: str | None = None,
    name: str | None = None,
    name_trigger: bool = False,
    mcp_config: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> None:
    """运行 joinly 客户端。

    参数:
        joinly_url (str | FastMCP): joinly 服务端 URL，或 FastMCP 实例。
        meeting_url (str): 要加入的会议 URL。
        llm_provider (str): 大语言模型提供方。
        llm_model (str): 大语言模型名称。
        prompt (str | None): 模型的系统提示词。
        prompt_style (str | None): 未提供自定义提示词时使用的默认提示类型。
        name (str | None): 参与者显示名称。
        name_trigger (bool): 是否仅在转写中出现名称时触发智能体。
        mcp_config (dict[str, Any] | None): 附加 MCP 服务器配置。
        settings (dict[str, Any] | None): 客户端的其它设置项。
    """
    client = JoinlyClient(
        joinly_url,
        name=name,
        name_trigger=name_trigger,
        settings=settings,
    )

    if mcp_config and "mcpServers" not in mcp_config:
        logger.warning(
            "MCP configuration does not contain 'mcpServers'. "
            "Using the main joinly client only."
        )
        mcp_config = None
    elif mcp_config and "joinly" in mcp_config["mcpServers"]:
        mcp_config["_joinly"] = mcp_config.pop("joinly")

    additional_clients = (
        {
            name: Client({"mcpServers": {name: config}})
            for name, config in mcp_config["mcpServers"].items()
        }
        if mcp_config
        else {}
    )

    async def log_segments(segments: list[TranscriptSegment]) -> None:
        """记录从客户端收到的片段。"""
        for segment in segments:
            logger.info('%s: "%s"', segment.speaker or "Participant", segment.text)

    client.add_segment_callback(log_segments)
    llm = get_llm(llm_provider, llm_model)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(client)
        for client_name, additional_client in additional_clients.items():
            logger.info("Connecting to %s", client_name)
            await stack.enter_async_context(additional_client)
            logger.debug("Connected to %s", client_name)

        joinly_config = McpClientConfig(client=client.client, exclude=["join_meeting"])
        tools, tool_executor = await load_tools(
            joinly_config
            if not additional_clients
            else {
                "joinly": joinly_config,
                **{
                    name: McpClientConfig(client)
                    for name, client in additional_clients.items()
                },
            }
        )
        agent = client.create_agent(
            llm,
            tools,
            tool_executor,
            prompt=get_prompt(
                instructions=prompt,
                prompt_style=prompt_style,
                name=client.name,
            ),
        )
        async with agent:
            await client.join_meeting(meeting_url)
            try:
                await asyncio.Event().wait()
            finally:
                usage = agent.usage.merge(await client.get_usage())
                if usage.root:
                    logger.info("Usage:\n%s", usage)


if __name__ == "__main__":
    cli()
