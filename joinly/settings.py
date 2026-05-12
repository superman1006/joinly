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
    """会议代理的配置项。"""

    name: str = Field(default="joinly")
    language: str = Field(default="en")
    device: str = Field(default="cpu")

    meeting_provider: str | type[MeetingProvider] = Field(default="browser")
    vad: str | type[VAD] = Field(default="silero")
    stt: str | type[STT] = Field(default="whisper")
    tts: str | type[TTS] = Field(default="kokoro")
    transcription_controller: str | type[TranscriptionController] = Field(
        default="default"
    )
    speech_controller: str | type[SpeechController] = Field(default="default")

    meeting_provider_args: dict[str, Any] = Field(default_factory=dict)
    vad_args: dict[str, Any] = Field(default_factory=dict)
    stt_args: dict[str, Any] = Field(default_factory=dict)
    tts_args: dict[str, Any] = Field(default_factory=dict)
    transcription_controller_args: dict[str, Any] = Field(default_factory=dict)
    speech_controller_args: dict[str, Any] = Field(default_factory=dict)

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
