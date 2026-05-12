import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click
from dotenv import load_dotenv

from joinly.server import mcp
from joinly.settings import Settings, set_settings
from joinly.utils.logging import configure_logging

logger = logging.getLogger(__name__)


def _parse_kv(
    _ctx: click.Context, _param: click.Parameter, value: tuple[str]
) -> dict[str, object]:
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
    return out


def _parse_mcp(
    _ctx: click.Context, _param: click.Parameter, value: tuple[str, ...]
) -> dict[str, dict[str, str]]:
    """将重复的 --mcp URL 解析为 名称→配置 的映射。"""
    servers: dict[str, dict[str, str]] = {}
    for url in value:
        name = urlparse(url).hostname or "mcp"
        servers[name] = {"url": url}
    return servers


@click.command()
@click.option(
    "--server/--client",
    help=(
        "以服务端或客户端模式运行 joinly。"
        "未提供会议 URL 时默认为服务端，否则为客户端。"
    ),
    default=None,
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
    "--language",
    "--lang",
    type=str,
    help="转写与语音合成所使用的语言。",
    default="en",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_LANGUAGE",
)
@click.option(
    "--device",
    type=str,
    help="模型运行设备。默认为 'cpu'，可设为 'cuda' 以使用 GPU。"
    "使用 'cuda' 需安装额外的 CUDA 相关依赖。",
    default="cpu",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_DEVICE",
)
@click.option(
    "-h",
    "--host",
    type=str,
    help="服务端绑定的主机地址。仅在与 --server 一起使用时生效。",
    default="127.0.0.1",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_SERVER_HOST",
)
@click.option(
    "-p",
    "--port",
    type=int,
    help="服务端绑定的端口。仅在与 --server 一起使用时生效。",
    default=8000,
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_SERVER_PORT",
)
@click.option(
    "--llm-provider",
    "--model-provider",
    type=str,
    help="客户端所用大语言模型的提供方。",
    default="openai",
    show_default=True,
    show_envvar=True,
    envvar=["JOINLY_LLM_PROVIDER", "JOINLY_MODEL_PROVIDER"],
)
@click.option(
    "--llm-model",
    "--model-name",
    type=str,
    help="客户端所用大语言模型的名称。",
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
    help="模型的系统提示词；未指定则使用默认系统提示词。",
    default=None,
    envvar="JOINLY_PROMPT",
)
@click.option(
    "--prompt-style",
    type=click.Choice(["dyadic", "mpc"], case_sensitive=False),
    help="未提供自定义提示词时使用的默认提示类型："
    "'dyadic' 适用于一对一会议，'mpc' 适用于多人会议。",
    default="mpc",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_PROMPT_STYLE",
)
@click.option(
    "--name-trigger",
    is_flag=True,
    help="仅在转写中出现本参与者名称时再触发智能体。"
    "仅在与 --client 一起使用时生效。建议将名称设为较常见、易被转写识别的词。",
)
@click.option(
    "-m",
    "--meeting-provider",
    type=str,
    help="要使用的会议提供方。",
    default="browser",
    show_default=True,
)
@click.option(
    "--vnc-server",
    is_flag=True,
    help="为会议提供方启用 VNC 服务。"
    "仅在与 --meeting-provider browser 一起使用时生效。",
    default=False,
    show_default=True,
)
@click.option(
    "--vnc-server-port",
    type=int,
    help="VNC 服务端口。仅在与 --vnc-server 一起使用时生效。",
    default=5900,
    show_default=True,
)
@click.option(
    "--vad",
    type=str,
    help='要使用的语音活动检测（VAD）服务，可选："webrtc"、"silero"。',
    default="silero",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_VAD",
)
@click.option(
    "--stt",
    type=str,
    help="要使用的语音转文字服务。"
    '可选："whisper"（本地）、"google"、"deepgram"。',
    default="whisper",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_STT",
)
@click.option(
    "--tts",
    type=str,
    help='要使用的文本转语音服务，可选："kokoro"（本地）、'
    '"elevenlabs"、"deepgram"、"google"、"resemble"。',
    default="kokoro",
    show_default=True,
    show_envvar=True,
    envvar="JOINLY_TTS",
)
@click.option(
    "--meeting-provider-arg",
    "meeting_provider_args",
    multiple=True,
    metavar="KEY=VAL",
    callback=_parse_kv,
    help="以 key=value 形式传入会议提供方参数，可多次指定。",
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
@click.option(
    "--logging-plain",
    is_flag=True,
    help="使用纯文本格式的日志输出。",
    show_envvar=True,
    envvar="JOINLY_LOGGING_PLAIN",
)
@click.option(
    "--mcp",
    "mcp_servers",
    multiple=True,
    type=str,
    callback=_parse_mcp,
    help="要连接的远程 MCP 服务器 URL，可多次指定。"
    "仅在与 --client 一起使用时生效。"
    "说明：在 Docker 内仅远程 HTTP 类服务器可用"
    "（不支持 stdio/npm 命令行与交互式 OAuth）。",
)
@click.option(
    "--mcp-config",
    "mcp_config_file",
    type=click.Path(dir_okay=False, readable=True),
    help="包含额外 MCP 服务器配置的 JSON 文件路径。"
    "仅在与 --client 一起使用时生效。"
    "说明：在 Docker 内该文件须挂载进容器，且仅远程 HTTP 类服务器可用"
    "（不支持 stdio/npm 命令行与交互式 OAuth）。",
    default=None,
)
@click.argument(
    "meeting-url",
    default=None,
    type=str,
    required=False,
)
def cli(  # noqa: PLR0913
    *,
    server: bool | None,
    host: str,
    port: int,
    llm_provider: str,
    llm_model: str,
    vnc_server: bool,
    vnc_server_port: int,
    prompt: str | None,
    prompt_style: str,
    name_trigger: bool,
    meeting_url: str | None,
    mcp_servers: dict[str, dict[str, str]],
    mcp_config_file: str | None,
    verbose: int,
    quiet: bool,
    logging_plain: bool,
    **cli_settings: Any,  # noqa: ANN401
) -> None:
    """启动 joinly MCP 服务端，或以服务端+客户端方式加入会议。"""
    if cli_settings.get("meeting_provider") == "browser" and vnc_server:
        cli_settings["meeting_provider_args"] = cli_settings.get(
            "meeting_provider_args", {}
        )
        cli_settings["meeting_provider_args"]["vnc_server"] = True
        cli_settings["meeting_provider_args"]["vnc_server_port"] = vnc_server_port

    settings = Settings(**cli_settings)  # type: ignore[arg-type]
    set_settings(settings)

    configure_logging(
        verbose=verbose,
        quiet=quiet,
        plain=logging_plain,
    )

    if server is True or (server is None and meeting_url is None):
        mcp.run(transport="streamable-http", host=host, port=port, show_banner=False)
    else:
        import joinly_client

        if not meeting_url:
            msg = (
                "Meeting URL is required when running as a client. "
                "Please provide it as an argument."
            )
            raise click.UsageError(msg)

        mcp_config: dict[str, Any] | None = None
        if mcp_config_file:
            mcp_config = json.loads(Path(mcp_config_file).read_text())
        if mcp_servers:
            if mcp_config is None:
                mcp_config = {"mcpServers": {}}
            mcp_config.setdefault("mcpServers", {}).update(mcp_servers)

        asyncio.run(
            joinly_client.run(
                joinly_url=mcp,
                meeting_url=meeting_url,
                llm_provider=llm_provider,
                llm_model=llm_model,
                prompt=prompt,
                prompt_style=prompt_style,
                name=settings.name,
                name_trigger=name_trigger,
                mcp_config=mcp_config,
            )
        )


if __name__ == "__main__":
    cli()
