import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from functools import partial
from typing import Self

import numpy as np
from faster_whisper import WhisperModel

from joinly.core import STT
from joinly.settings import get_settings
from joinly.types import (
    AudioFormat,
    SpeechWindow,
    TranscriptSegment,
)
from joinly.utils.audio import calculate_audio_duration

logger = logging.getLogger(__name__)


class WhisperSTT(STT):
    """使用 Whisper 将音频转写为文字的类。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        model_name: str | None = None,
        compute_type: str = "auto",
        min_audio: float = 0.4,
        min_silence: float = 0.2,
        hotwords: list[str] | None = None,
        no_speech_threshold: float = 0.6,
    ) -> None:
        """初始化 WhisperSTT。

        参数:
            model_name: Whisper 模型名（默认 None：CPU 用 "base"，
                CUDA 用 "distil-large-v3"）。
            compute_type: 模型计算类型（默认 "auto"）。
            min_audio: 参与转写的最短音频时长（秒）。
            min_silence: 结束片段前所需的最短静音时长（秒）。
            hotwords: 用于提升转写准确率的热词列表。
            no_speech_threshold: 过滤 Whisper 幻觉的 no_speech_prob 阈值（默认 0.6）。
        """
        self.no_speech_threshold = no_speech_threshold
        self.model_name = model_name or (
            "distil-large-v3" if get_settings().device == "cuda" else "base"
        )
        self._set_model_name = model_name is not None
        self.compute_type = compute_type
        self.min_audio = min_audio
        self.min_silence = min_silence
        hotwords_arr = (hotwords or []) + [get_settings().name]
        self._hotwords_str = " ".join(hotwords_arr)
        self.audio_format = AudioFormat(sample_rate=16000, byte_depth=4)
        self._model: WhisperModel | None = None
        self._sem = asyncio.BoundedSemaphore(1)

    async def __aenter__(self) -> Self:
        """初始化 Whisper 模型。"""
        logger.info(
            "Initializing Whisper model: %s, device: %s, compute type: %s",
            self.model_name,
            get_settings().device,
            self.compute_type,
        )

        self._model = await asyncio.to_thread(
            WhisperModel,
            self.model_name,
            device=get_settings().device,
            compute_type=self.compute_type,
            local_files_only=not self._set_model_name,
        )

        logger.debug("Initialized Whisper model")

        return self

    async def __aexit__(self, *_exc: object) -> None:
        """停止处理器时清理资源。"""
        if self._model is not None:
            del self._model
            self._model = None

    async def stream(
        self, windows: AsyncIterator[SpeechWindow]
    ) -> AsyncIterator[TranscriptSegment]:
        """流式处理音频窗口并产生转写片段。

        参数:
            windows: SpeechWindow 的异步迭代器。

        产生:
            TranscriptSegment: 转写得到的片段。
        """
        if self._model is None:
            msg = "Model not initialized"
            raise RuntimeError(msg)

        queue = asyncio.Queue[tuple[bytes, float, float, str | None] | None](maxsize=10)
        buffer_task = asyncio.create_task(self._buffer_windows(windows, queue))

        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                data, start, end, speaker = item
                async for segment in self._transcribe(data, start, end, speaker):
                    yield segment
        finally:
            buffer_task.cancel()

    async def _buffer_windows(
        self,
        windows: AsyncIterator[SpeechWindow],
        queue: asyncio.Queue[tuple[bytes, float, float, str | None] | None],
    ) -> None:
        """将音频窗口缓冲入队列。

        参数:
            windows: SpeechWindow 的异步迭代器。
            queue: 用于放入缓冲后音频块的队列。
        """
        buffer = bytearray()
        start: float | None = None
        speakers: defaultdict[str, float] = defaultdict(int)
        silence_bytes: int = 0
        byte_per_second: int = (
            self.audio_format.sample_rate * self.audio_format.byte_depth
        )
        min_bytes: int = int(byte_per_second * self.min_audio)
        min_silence_bytes: int = int(byte_per_second * self.min_silence)

        async for window in windows:
            if window.is_speech and start is None:
                start = window.time_ns / 1e9

            if start is not None:
                buffer.extend(window.data)
                if window.is_speech:
                    silence_bytes = 0
                    if window.speaker is not None:
                        speakers[window.speaker] += calculate_audio_duration(
                            len(window.data), self.audio_format
                        )
                else:
                    silence_bytes += len(window.data)

                if len(buffer) >= min_bytes and silence_bytes >= min_silence_bytes:
                    end = start + int(len(buffer) / byte_per_second)
                    speaker, speaker_time = max(
                        speakers.items(),
                        key=lambda x: x[1],
                        default=(None, 0),
                    )
                    if speaker_time < 0.1 * (end - start):
                        speaker = None
                    await queue.put((bytes(buffer), start, end, speaker))
                    buffer.clear()
                    start = None
                    speakers.clear()
                    silence_bytes = 0

        if start is not None and buffer:
            end = start + int(len(buffer) / byte_per_second)
            speaker = max(speakers.items(), key=lambda item: item[1])[0]
            await queue.put((bytes(buffer), start, end, speaker))
        await queue.put(None)

    async def _transcribe(
        self,
        data: bytes,
        start: float,
        end: float | None = None,
        speaker: str | None = None,
    ) -> AsyncIterator[TranscriptSegment]:
        """处理输入音频块并产生转写结果。

        参数:
            data: 字节格式的音频数据。
            start: 音频片段的起始时间。
            end: 音频片段的结束时间。
            speaker: 说话人标识。

        产生:
            TranscriptSegment: 转写得到的片段。
        """
        if self._model is None:
            msg = "Model not initialized"
            raise RuntimeError(msg)

        async with self._sem:
            logger.debug(
                "Processing audio chunk of size: %d (%.2fs)",
                len(data),
                calculate_audio_duration(len(data), self.audio_format),
            )

            audio_segment = np.frombuffer(data, dtype=np.float32)
            segments, _ = await asyncio.to_thread(
                self._model.transcribe,
                audio_segment,
                language=get_settings().language,
                beam_size=5,
                condition_on_previous_text=False,
                hotwords=self._hotwords_str,
            )

            get_next_segment = partial(next, iter(segments), None)
            while True:
                seg = await asyncio.to_thread(get_next_segment)
                if seg is None:
                    break

                text = seg.text.strip()
                # 过滤幻觉：no_speech_prob 超过阈值表示该段极可能是静音/噪声
                if text and seg.no_speech_prob < self.no_speech_threshold:
                    yield TranscriptSegment(
                        text=text,
                        start=min(start + seg.start, end or float("inf")),
                        end=min(start + seg.end, end or float("inf")),
                        speaker=speaker,
                    )
