from dataclasses import dataclass
from typing import Literal

from joinly_common.types import (
    MeetingChatHistory,
    MeetingChatMessage,
    MeetingParticipant,
    MeetingParticipantList,
    ServiceUsage,
    SpeakerRole,
    Transcript,
    TranscriptSegment,
    UIAnimationContent,
    UIHtmlContent,
    UIUpdate,
    Usage,
    VideoSnapshot,
)

ActionAnimation = Literal["typing", "reading", "interrupted", "sharing"]

__all__ = [
    "ActionAnimation",
    "MeetingChatHistory",
    "MeetingChatMessage",
    "MeetingParticipant",
    "MeetingParticipantList",
    "ServiceUsage",
    "SpeakerRole",
    "Transcript",
    "TranscriptSegment",
    "UIAnimationContent",
    "UIHtmlContent",
    "UIUpdate",
    "Usage",
    "VideoSnapshot",
]


class ProviderNotSupportedError(Exception):
    """当提供方不支持所请求的功能时抛出。"""


class IncompatibleAudioFormatError(Exception):
    """当音频格式与期望或给定格式不兼容时抛出。"""


class SpeechInterruptedError(Exception):
    """当语音因检测到说话声而被中断时抛出。"""

    _TEMPLATE = 'Interrupted by detected speech. Spoken until now: "%s..."'

    def __init__(self, spoken_text: str = "") -> None:
        """使用已朗读文本初始化 SpeechInterruptedError。"""
        self.spoken_text: str = spoken_text
        super().__init__(self.__str__())

    def __str__(self) -> str:
        """返回错误的字符串表示。"""
        return self._TEMPLATE % self.spoken_text


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """PCM 音频的属性。

    属性:
        sample_rate (int): 音频流的采样率（Hz）。
        byte_depth (int): 音频流的字节深度（字节）。
    """

    sample_rate: int
    byte_depth: int


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """表示一块音频数据。

    属性:
        data (bytes): 原始 PCM 音频数据。
        time_ns (int): 音频块的时间戳（纳秒）。
        speaker (str | None): 若可识别，表示该音频块的主要说话人。
    """

    data: bytes
    time_ns: int
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class SpeechWindow:
    """表示带语音活动检测的音频窗口。

    属性:
        data (bytes): 该窗口的原始 PCM 音频数据。
        time_ns (int): 音频窗口的时间戳（纳秒）。
        is_speech (bool): 窗口内是否包含语音。
        speaker (str | None): 若可识别，表示该音频窗口的说话人。
    """

    data: bytes
    time_ns: int
    is_speech: bool
    speaker: str | None = None
