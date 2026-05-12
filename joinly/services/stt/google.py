import asyncio
import io
import logging
import os
import wave
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Self

from google import genai
from google.genai import types

from joinly.core import STT
from joinly.types import (
    AudioFormat,
    SpeechWindow,
    TranscriptSegment,
)
from joinly.utils.audio import calculate_audio_duration
from joinly.utils.usage import add_usage

logger = logging.getLogger(__name__)


class GoogleSTT(STT):
    """基于 Gemini 音频理解 API 的 STT 服务。"""

    def __init__(
        self,
        *,
        model_name: str = "gemini-2.5-flash",
        prompt: str = "Generate a transcript of the speech.",
        sample_rate: int = 16000,
    ) -> None:
        """初始化 Gemini STT 服务。

        参数:
            model_name: 用于音频理解的 Gemini 模型名。
            prompt: 与音频一并发送、用于请求转写的提示词。
            sample_rate: 音频采样率（默认 16000）。
        """
        if os.getenv("GEMINI_API_KEY") is None and os.getenv("GOOGLE_API_KEY") is None:
            msg = "GEMINI_API_KEY or GOOGLE_API_KEY must be set in the environment."
            raise ValueError(msg)

        self._model = model_name
        self._prompt = prompt
        self._client: genai.Client | None = None
        self._lock = asyncio.Lock()

        # Gemini 会将音频下采样到 16kHz 再处理
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def __aenter__(self) -> Self:
        """初始化 Gemini 客户端。"""
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self._client = genai.Client(api_key=api_key)

        logger.info("Initialized Gemini STT with model: %s", self._model)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """清理资源。"""
        self._client = None

    async def stream(
        self, windows: AsyncIterator[SpeechWindow]
    ) -> AsyncIterator[TranscriptSegment]:
        """使用 Gemini 音频理解能力转写音频流。

        说明：Gemini 音频理解 API 并非流式接口。
        本方法会缓冲完整音频流，再作为单次请求发送进行转写。

        参数:
            windows: 待转写的音频窗口异步迭代器。

        产生:
            TranscriptSegment: 转写得到的片段（可能多个）。
        """
        if self._client is None:
            msg = "STT service is not initialized."
            raise RuntimeError(msg)

        # 缓冲完整音频流
        start_time: float | None = None
        end_time: float = 0.0
        audio_buffer = bytearray()
        speakers: defaultdict[str, float] = defaultdict(float)

        async for window in windows:
            if start_time is None:
                start_time = window.time_ns / 1e9

            audio_buffer.extend(window.data)

            duration = calculate_audio_duration(len(window.data), self.audio_format)
            end_time = (window.time_ns / 1e9) + duration
            if window.speaker:
                speakers[window.speaker] += duration

        if not audio_buffer:
            logger.warning("Received no audio data to transcribe.")
            return

        # 将 PCM 转为 WAV 格式
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(self.audio_format.byte_depth)
            wf.setframerate(self.audio_format.sample_rate)
            wf.writeframes(audio_buffer)
        wav_buffer.seek(0)
        audio_bytes = wav_buffer.getvalue()

        # 发送到 Gemini API
        async with self._lock:
            audio_duration_secs = calculate_audio_duration(
                len(audio_buffer), self.audio_format
            )
            logger.debug(
                "Sending %.2f seconds of audio to Gemini for transcription.",
                audio_duration_secs,
            )

            try:
                # 使用内联音频数据
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=[
                        self._prompt,
                        types.Part.from_bytes(
                            data=audio_bytes,
                            mime_type="audio/wav",
                        ),
                    ],
                )

                transcribed_text = (response.text or "").strip()

                # 记录用量
                add_usage(
                    service="gemini_stt",
                    usage={"seconds": audio_duration_secs},
                    meta={"model": self._model},
                )

                if transcribed_text:
                    # 判断主要说话人
                    speaker = (
                        max(speakers.items(), key=lambda item: item[1])[0]
                        if speakers
                        else None
                    )

                    yield TranscriptSegment(
                        text=transcribed_text,
                        start=start_time or 0.0,
                        end=end_time,
                        speaker=speaker,
                    )
                else:
                    logger.info("Gemini returned an empty transcription.")

            except Exception as e:
                logger.exception("Error during Gemini transcription")
                msg = f"Failed to transcribe audio with Gemini: {e}"
                raise RuntimeError(msg) from e
