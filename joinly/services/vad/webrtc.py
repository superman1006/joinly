import logging
from typing import Self

import webrtcvad

from joinly.services.vad.base import BasePaddedVAD
from joinly.types import AudioFormat

logger = logging.getLogger(__name__)


class WebrtcVAD(BasePaddedVAD):
    """基于 webrtcvad 的语音活动检测。"""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        window_duration: int = 30,
        aggressiveness: int = 3,
    ) -> None:
        """初始化 webrtc VAD。

        参数:
            sample_rate: 音频数据的采样率（默认 16000）。
            window_duration: 每个分析窗口的时长（毫秒，默认 30），
                决定 VAD 处理的块大小。
            aggressiveness: VAD 激进程度（0–3，默认 3）。
        """
        if sample_rate not in (8000, 16000, 32000, 48000):
            msg = (
                f"Unsupported sample rate {sample_rate}. "
                "Supported sample rates are 8000, 16000, 32000, and 48000."
            )
            raise ValueError(msg)
        if window_duration not in (10, 20, 30):
            msg = (
                f"Unsupported window duration {window_duration}. "
                "Supported window durations are 10, 20, and 30 milliseconds."
            )
            raise ValueError(msg)

        self._sample_rate = sample_rate
        self._window_duration = window_duration
        self._aggressiveness = aggressiveness
        self._window_size_samples = int(
            self._sample_rate * self._window_duration / 1000
        )
        self._vad: webrtcvad.Vad | None = None
        self.audio_format = AudioFormat(sample_rate=self._sample_rate, byte_depth=2)

    async def __aenter__(self) -> Self:
        """初始化 webrtc VAD。"""
        self._vad = webrtcvad.Vad(self._aggressiveness)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """清理资源。"""
        if self._vad is not None:
            del self._vad
            self._vad = None

    @property
    def window_size_samples(self) -> int:
        """期望的窗口大小（采样点数）。"""
        return self._window_size_samples

    async def is_speech(self, window: bytes) -> bool:
        """判断给定音频窗口是否包含语音。

        参数:
            window: 待检测的音频窗口。

        返回:
            bool: 若包含语音则为 True，否则为 False。
        """
        if self._vad is None:
            msg = "VAD is not initialized"
            raise RuntimeError(msg)

        return self._vad.is_speech(window, self._sample_rate, self._window_size_samples)
