"""默认语音输出控制器（DefaultSpeechController）。

将 Agent 文本分块 → ``TTS.stream`` 预取 → 格式转换 → ``AudioWriter`` 写入虚拟麦克风。
支持 barge-in：``no_speech_event`` 被转写控制器清除时中断当前朗读。

``tts_active_event`` 由 container 注入，朗读期间 set，供转写侧抑制回声。
"""

import asyncio
import logging
from typing import Self, cast

from semchunk.semchunk import chunkerify

from joinly.core import TTS, AudioWriter, SpeechController
from joinly.settings import get_settings
from joinly.types import (
    AudioFormat,
    SpeakerRole,
    SpeechInterruptedError,
    Transcript,
    TranscriptSegment,
)
from joinly.utils.audio import calculate_audio_duration, convert_audio_format
from joinly.utils.clock import Clock
from joinly.utils.events import EventBus, EventType

logger = logging.getLogger(__name__)

_CHUNK_END = object()
_TEXT_END = object()


class DefaultSpeechController(SpeechController):
    """默认 TTS 朗读流程：生产者/消费者双任务，带预取与插话检测。"""

    writer: AudioWriter
    tts: TTS
    no_speech_event: asyncio.Event
    # 由外部（container）注入，TTS 期间设为 set 以抑制 STT 回声
    tts_active_event: asyncio.Event | None = None

    def __init__(
        self,
        *,
        prefetch_chunks: int = 2,
    ) -> None:
        """初始化 SpeechFlowController。

        参数:
            prefetch_chunks (int): 语音合成时预取的块数量（默认 2）。
        """
        self.prefetch_chunks = int(prefetch_chunks)
        self._clock: Clock | None = None
        self._transcript: Transcript | None = None
        self._lock = asyncio.Lock()
        self._event_bus: EventBus | None = None

    async def __aenter__(self) -> Self:
        """进入音频流上下文。"""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """停止音频流并清理资源。"""
        await self.stop()

    async def start(
        self, clock: Clock, transcript: Transcript, event_bus: EventBus
    ) -> None:
        """启动语音输出控制器。

        参数:
            clock (Clock): 用于计时的时钟。
            transcript (Transcript): 用于写入语音相关记录的 Transcript 对象。
            event_bus (EventBus): 用于发布事件的总线。
        """
        if self._clock is not None or self._transcript is not None:
            msg = "Speech controller already active"
            raise RuntimeError(msg)

        self._clock = clock
        self._transcript = transcript
        self._event_bus = event_bus

    async def stop(self) -> None:
        """停止语音输出控制器。"""
        self._clock = None
        self._transcript = None
        self._event_bus = None

    def _notify(self, event_type: EventType) -> None:
        """向事件总线通知事件。

        参数:
            event_type (EventType): 要发布的事件类型。
        """
        if self._event_bus is None:
            return

        self._event_bus.publish(event_type)

    async def speak_text(self, text: str) -> None:
        """通过虚拟麦克风朗读给定文本。

        参数:
            text (str): 要朗读的文本。
        """
        if self.tts_active_event is not None:
            self.tts_active_event.set()
        try:
            async with self._lock, asyncio.TaskGroup() as tg:
                chunks: list[str] = await self._chunk_text(text)
                audio_queue: asyncio.Queue[bytes | object] = asyncio.Queue()
                prefetch_sem = asyncio.Semaphore(self.prefetch_chunks)
                tg.create_task(
                    self._speech_producer(
                        chunks,
                        audio_queue,
                        prefetch_sem,
                    )
                )
                tg.create_task(
                    self._speech_consumer(
                        chunks,
                        audio_queue,
                        prefetch_sem,
                    )
                )
        except* SpeechInterruptedError as eg:
            raise eg.exceptions[0] from None
        except* Exception as eg:
            msg = "Error while speaking text"
            logger.exception(msg)
            raise RuntimeError(msg) from eg
        finally:
            if self.tts_active_event is not None:
                self.tts_active_event.clear()

    async def _chunk_text(self, text: str) -> list[str]:
        """将文本切分为较小片段以便处理。

        参数:
            text (str): 待分块的文本。

        返回:
            list[str]: 文本片段列表。
        """
        chunker = chunkerify(
            lambda s: len(s.split()),
            chunk_size=max(15, min(50, int(0.2 * len(text.split())))),
        )
        chunks: list[str] = await asyncio.to_thread(chunker, text)  # type: ignore[operator]
        return chunks

    async def _speech_producer(
        self,
        chunks: list[str],
        audio_queue: asyncio.Queue[bytes | object],
        prefetch_sem: asyncio.Semaphore,
    ) -> None:
        """生成语音片段并放入队列。

        参数:
            chunks (list[str]): 按片段朗读的文本。
            audio_queue (asyncio.Queue[bytes | object]): 用于放入语音片段的队列。
            prefetch_sem (asyncio.Semaphore): 限制预取块数量的信号量。
        """
        for chunk in chunks:
            await prefetch_sem.acquire()
            async for segment in self.tts.stream(chunk):
                await audio_queue.put(segment)
            await audio_queue.put(_CHUNK_END)
        await audio_queue.put(_TEXT_END)

    async def _speech_consumer(
        self,
        chunks: list[str],
        audio_queue: asyncio.Queue[bytes | object],
        prefetch_sem: asyncio.Semaphore,
    ) -> None:
        """通过虚拟麦克风播放给定音频。

        参数:
            chunks (list[str]): 按片段朗读的文本。
            audio_queue (asyncio.Queue[bytes | object]): 用于获取音频片段的队列。
            prefetch_sem (asyncio.Semaphore): 限制预取块数量的信号量。

        引发:
            SpeechInterruptedError: 当语音被打断时。
        """
        if self._transcript is None or self._clock is None:
            msg = "Speech controller not active"
            raise RuntimeError(msg)

        chunk_idx: int = 0
        byte_size: int = 0
        start = self._clock.now_s
        buffer = bytearray()

        while True:
            segment = await audio_queue.get()

            if segment is _TEXT_END:
                break

            if segment is _CHUNK_END:
                if buffer:
                    await self.writer.write(bytes(buffer))
                    buffer.clear()
                self._transcript.add_segment(
                    TranscriptSegment(
                        text=chunks[chunk_idx],
                        start=start,
                        end=self._clock.now_s,
                        speaker=get_settings().name,
                        role=SpeakerRole.assistant,
                    )
                )
                self._notify("segment")
                prefetch_sem.release()
                logger.debug(
                    'Spoken (%d/%d): "%s"',
                    chunk_idx + 1,
                    len(chunks),
                    chunks[chunk_idx],
                )
                chunk_idx += 1
                byte_size = 0
                continue

            buffer.extend(
                convert_audio_format(
                    cast("bytes", segment),
                    self.tts.audio_format,
                    self.writer.audio_format,
                )
            )

            while len(buffer) >= self.writer.chunk_size:
                # 检查是否发生语音打断（barge-in）
                if not self.no_speech_event.is_set():
                    estimated_text = await self._estimate_spoken_text(
                        chunks[chunk_idx], byte_size, self.writer.audio_format
                    )
                    logger.debug(
                        'Spoken (%d/%d): "%s" (interrupted)',
                        chunk_idx + 1,
                        len(chunks),
                        estimated_text,
                    )
                    spoken_text = " ".join([*chunks[:chunk_idx], estimated_text])
                    if spoken_text:
                        self._transcript.add_segment(
                            TranscriptSegment(
                                text=estimated_text + "...",
                                start=start if byte_size > 0 else self._clock.now_s,
                                end=self._clock.now_s,
                                speaker=get_settings().name,
                                role=SpeakerRole.assistant,
                            )
                        )
                        self._notify("segment")
                    raise SpeechInterruptedError(spoken_text=spoken_text)

                await self.writer.write(bytes(buffer[: self.writer.chunk_size]))
                if byte_size == 0:
                    start = self._clock.now_s
                byte_size += self.writer.chunk_size
                del buffer[: self.writer.chunk_size]

    async def _estimate_spoken_text(
        self, text: str, audio_byte_size: int, audio_format: AudioFormat
    ) -> str:
        """根据字节大小与音频格式估算已朗读文本。

        参数:
            text (str): 要朗读的文本。
            audio_byte_size (int): 音频数据的字节长度。
            audio_format (AudioFormat): 语音的音频格式。

        返回:
            str: 估算的已朗读文本。
        """
        wps = 2.0  # 粗略按每秒词数估算
        audio_duration = calculate_audio_duration(audio_byte_size, audio_format)
        word_num = int(audio_duration * wps)
        words = text.split(" ")
        return " ".join(words[: min(word_num, len(words))])
