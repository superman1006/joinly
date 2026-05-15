"""joinly 运行时配置（Settings）。

通过 ``JOINLY_`` 前缀的环境变量加载，支持嵌套参数（``JOINLY_STT_ARGS__model_name``）。
每个 MCP 客户端连接可通过 HTTP 头 ``joinly-settings`` 覆盖部分配置（见 ``server.py``）。

使用 ``get_settings()`` / ``set_settings()`` / ``reset_settings()`` 在 ContextVar 中
按连接隔离配置，保证多客户端并发时互不干扰。
"""

from contextvars import ContextVar, Token
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from joinly.core import (
    STT,
    TTS,
    VAD,
    MeetingProvider,
    SpeechController,
    TranscriptionController,
)


class Settings(BaseSettings):
    """会议代理的配置项。

    短令牌（如 ``stt="aliyun"``）由 ``SessionContainer`` 解析为
    ``joinly.services.stt.aliyun.AliyunSTT`` 等具体类。
  """

    name: str = Field(default="joinly", description="会议中显示的参与者名称")
    language: str = Field(
        default="en", description="STT/TTS 使用的语言代码（如 zh、en）"
    )
    device: str = Field(
        default="cpu", description="本地模型运行设备：cpu 或 cuda"
    )

    meeting_provider: str | type[MeetingProvider] = Field(
        default="browser", description="会议提供方短令牌或类"
    )
    vad: str | type[VAD] = Field(default="silero", description="VAD 短令牌或类")
    stt: str | type[STT] = Field(
        default="whisper", description="STT 短令牌（whisper/aliyun/google/deepgram）"
    )
    tts: str | type[TTS] = Field(
        default="kokoro", description="TTS 短令牌（kokoro/aliyun/google/...）"
    )
    transcription_controller: str | type[TranscriptionController] = Field(
        default="default", description="转写控制器短令牌"
    )
    speech_controller: str | type[SpeechController] = Field(
        default="default", description="语音输出控制器短令牌"
    )

    meeting_provider_args: dict[str, Any] = Field(
        default_factory=dict, description="传给 MeetingProvider 构造函数的额外参数"
    )
    vad_args: dict[str, Any] = Field(
        default_factory=dict, description="传给 VAD 构造函数的额外参数"
    )
    stt_args: dict[str, Any] = Field(
        default_factory=dict, description="传给 STT 构造函数的额外参数"
    )
    tts_args: dict[str, Any] = Field(
        default_factory=dict, description="传给 TTS 构造函数的额外参数"
    )
    transcription_controller_args: dict[str, Any] = Field(
        default_factory=dict,
        description="传给 TranscriptionController 的参数（如 utterance_tail_seconds）",
    )
    speech_controller_args: dict[str, Any] = Field(
        default_factory=dict,
        description="传给 SpeechController 的参数（如 prefetch_chunks）",
    )

    model_config = SettingsConfigDict(
        env_prefix="JOINLY_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
    )


_current_settings: ContextVar[Settings] = ContextVar("settings", default=Settings())  # noqa: B039


def get_settings() -> Settings:
    """获取当前配置。

    返回:
        Settings: 当前配置。
    """
    return _current_settings.get()


def set_settings(settings: Settings) -> Token[Settings]:
    """设置当前配置。

    参数:
        settings (Settings): 要设置的配置。

    返回:
        Token[Settings]: 可用于恢复先前配置的令牌。
    """
    return _current_settings.set(settings)


def reset_settings(token: Token[Settings]) -> None:
    """将当前配置恢复为上一值。

    参数:
        token (Token[Settings]): `set_settings` 返回的令牌。
    """
    _current_settings.reset(token)
