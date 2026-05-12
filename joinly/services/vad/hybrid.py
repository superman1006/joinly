import logging
from contextlib import AsyncExitStack
from typing import Self

from joinly.services.vad.base import BasePaddedVAD
from joinly.services.vad.silero import SileroVAD
from joinly.services.vad.webrtc import WebrtcVAD
from joinly.types import AudioFormat
from joinly.utils.audio import convert_audio_format

logger = logging.getLogger(__name__)


class HybridVAD(BasePaddedVAD):
    """结合 Silero 与 WebRTC 的混合 VAD。

    主要使用 WebRTC 以获得更高计算效率；在静音段之后对 WebRTC 首次检出的语音
    再用 Silero 复核，以减少误检。
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        webrtc_aggressiveness: int = 3,
        silero_speech_threshold: float = 0.75,
    ) -> None:
        """混合 VAD 初始化。

        参数:
            sample_rate (int, optional): 音频采样率，默认 16000。
            webrtc_aggressiveness (int, optional): WebRTC VAD 激进程度，默认 3。
            silero_speech_threshold (float, optional): Silero 语音阈值，默认 0.75。
        """
        self._webrtc = WebrtcVAD(
            sample_rate=sample_rate,
            window_duration=30,
            aggressiveness=webrtc_aggressiveness,
        )
        self._silero = SileroVAD(
            sample_rate=sample_rate,
            speech_threshold=silero_speech_threshold,
            use_state=True,
        )
        self.audio_format = AudioFormat(
            sample_rate=sample_rate, byte_depth=self._webrtc.audio_format.byte_depth
        )
        self._last_is_speech: bool = False
        self._last_used_silero: bool = False
        self._padding = (
            b"\x00"
            * self._webrtc.audio_format.byte_depth
            * (self._silero.window_size_samples - self._webrtc.window_size_samples)
        )
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> Self:
        """初始化混合 VAD。"""
        self._last_is_speech = False
        self._last_used_silero = False
        await self._stack.enter_async_context(self._webrtc)
        await self._stack.enter_async_context(self._silero)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """清理资源。"""
        await self._stack.aclose()

    @property
    def window_size_samples(self) -> int:
        """期望的窗口大小（采样点数）。"""
        return self._webrtc.window_size_samples

    async def is_speech(self, window: bytes) -> bool:
        """判断音频窗口是否包含语音。

        主要使用 WebRTC 做高效检测；在一段静音之后，对 WebRTC 检出的首次语音
        会再用 Silero 确认，以降低误报。

        参数:
            window (bytes): 待检测的音频窗口。

        返回:
            bool: 若包含语音则为 True，否则为 False。
        """
        is_speech = await self._webrtc.is_speech(window)

        if is_speech and not self._last_is_speech:
            is_speech = await self._silero_is_speech(window)
        else:
            self._last_used_silero = False

        self._last_is_speech = is_speech
        return is_speech

    async def _silero_is_speech(self, window: bytes) -> bool:
        """使用 Silero VAD 判断音频窗口是否包含语音。"""
        if not self._last_used_silero:
            self._silero.reset_state()
        self._last_used_silero = True
        return await self._silero.is_speech(
            convert_audio_format(
                self._padding + window,
                self._webrtc.audio_format,
                self._silero.audio_format,
            )
        )
