"""默认转写控制器（DefaultTranscriptionController）。

音频管线: ``AudioReader`` → 格式转换 → ``VAD.stream`` → 按话语分窗 → ``STT.stream``
→ 写入 ``Transcript`` 并发布 ``segment`` / ``utterance`` 事件。

防回声 + Barge-in：
    ``tts_active_event`` 在 TTS 播放期间为 set。此期间：
    1. 不创建新的 STT 任务（避免 bot 自身回声被转写），
    2. 已收到的 STT 结果被丢弃，
    3. 但 VAD 仍在工作；用户持续说话超过 ``barge_in_delay`` 秒时
       清除 ``no_speech_event`` —— ``DefaultSpeechController`` 检测到后
       抛出 ``SpeechInterruptedError`` 中断当前 TTS。
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from typing import Self

from joinly.core import STT, VAD, AudioReader, TranscriptionController
from joinly.types import AudioChunk, SpeechWindow, Transcript, TranscriptSegment
from joinly.utils.audio import calculate_audio_duration, convert_audio_format
from joinly.utils.clock import Clock
from joinly.utils.events import EventBus, EventType

logger = logging.getLogger(__name__)


class DefaultTranscriptionController(TranscriptionController):
    """默认转写流程实现。

    后台 ``_vad_worker`` 持续读取会议音频；检测到话语起止后，为每段话语启动独立
    ``_stt_utterance`` 任务（并发上限 ``max_stt_tasks``）。
    """

    reader: AudioReader
    vad: VAD
    stt: STT

    def __init__(
        self,
        *,
        utterance_tail_seconds: float = 0.6,
        no_speech_event_delay: float = 0.4,
        barge_in_delay: float = 0.6,
        max_stt_tasks: int = 5,
        window_queue_size: int = 100,
    ) -> None:
        """初始化 TranscriptionController。

        参数:
            utterance_tail_seconds (float): 最后一次检测到语音后，再等待多少秒视为
                话语结束（默认 0.6）。
            no_speech_event_delay (float): 触发「无语音」事件前的等待秒数（默认 0.4）。
            barge_in_delay (float): TTS 播放期间，持续多少秒语音才视为打断
                （默认 0.6，比常规 delay 长一点以过滤回声）。
            max_stt_tasks (int): 并发 STT 任务上限（默认 5）。
            window_queue_size (int): 窗口队列最大长度（默认 100）。
        """
        self.utterance_tail_seconds = float(utterance_tail_seconds)
        self.no_speech_event_delay = float(no_speech_event_delay)
        self.barge_in_delay = float(barge_in_delay)
        self.max_stt_tasks = max_stt_tasks
        self.window_queue_size = window_queue_size
        self._vad_task: asyncio.Task | None = None
        self._window_queue: asyncio.Queue[SpeechWindow | None] | None = None
        self._stt_tasks: set[asyncio.Task] = set()
        self._no_speech_event = asyncio.Event()
        # TTS 播放期间为 set；STT 结果输出前检查，防止回声被转写
        self.tts_active_event = asyncio.Event()
        self._clock: Clock | None = None
        self._transcript: Transcript | None = None
        self._event_bus: EventBus | None = None

    @property
    def no_speech_event(self) -> asyncio.Event:
        """获取在未检测到语音时被设置的事件。"""
        return self._no_speech_event

    async def __aenter__(self) -> Self:
        """进入转写控制器。"""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """清理转写控制器。"""
        await self.stop()

    async def start(
        self, clock: Clock, transcript: Transcript, event_bus: EventBus
    ) -> None:
        """使用给定的 reader、vad 与 stt 启动转写控制器。

        参数:
            clock (Clock): 用于计时的时钟。
            transcript (Transcript): 用于保存转写片段的 Transcript 对象。
            event_bus (EventBus): 用于发布事件的总线。
        """
        if self._vad_task is not None:
            msg = "Transcription controller already started"
            raise RuntimeError(msg)

        self._no_speech_event.set()
        self._clock = clock
        self._transcript = transcript
        self._event_bus = event_bus
        self._vad_task = asyncio.create_task(self._vad_worker())

    async def stop(self) -> None:
        """停止转写控制器并清理资源。"""
        if self._vad_task is not None:
            self._vad_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._vad_task
            self._vad_task = None

        self._no_speech_event.clear()

        for task in list(self._stt_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._stt_tasks.clear()

        self._clock = None
        self._transcript = None
        self._event_bus = None
        self._window_queue = None

    def _notify(self, event_type: EventType) -> None:
        """向事件总线通知事件。

        参数:
            event_type (EventType): 要发布的事件类型。
        """
        if self._event_bus is None:
            return

        self._event_bus.publish(event_type)

    async def _vad_worker(self) -> None:  # noqa: C901, PLR0915
        """主循环：把音频喂给 VAD，按话语切分后启动 STT 任务并维护 barge-in 信号。

        关键状态变量：
            ``last_speech``: 最近一次检测到语音的时间戳。用于判定话语结束
                （连续静音超过 ``utterance_tail_seconds``）。
            ``utterance_start``: 当前话语的起点。**TTS 期间也会被赋值**，
                用于在持续语音 ≥ ``barge_in_delay`` 时触发打断。
            ``self._window_queue``: 当前活跃的 STT 任务队列；TTS 播放期间不创建
                （保持 ``None``），避免回声被转写。
        """
        self._window_queue = None
        last_speech: int | None = None
        utterance_start: int | None = None
        dropped_windows: int = 0

        async def _chunk_iterator() -> AsyncIterator[AudioChunk]:
            """从 reader 产生音频块。"""
            offset: int | None = None
            while True:
                chunk = await self.reader.read()
                if offset is None:
                    offset = chunk.time_ns
                now_ns = chunk.time_ns - offset
                if self._clock is not None:
                    self._clock.update(chunk.time_ns - offset)
                yield AudioChunk(
                    data=convert_audio_format(
                        chunk.data, self.reader.audio_format, self.vad.audio_format
                    ),
                    time_ns=now_ns,
                    speaker=chunk.speaker,
                )

        vad_stream = self.vad.stream(_chunk_iterator())
        async for window in vad_stream:
            if window.is_speech:
                last_speech = window.time_ns
                # 即便 TTS 播放中也要追踪起点，用于 barge-in 检测
                if utterance_start is None:
                    utterance_start = window.time_ns

            if window.is_speech and self._window_queue is None:
                if self.tts_active_event.is_set():
                    # TTS 播放中：不创建 STT 任务（避免回声被转写），
                    # 但下面仍会根据 utterance_start 触发 barge-in。
                    logger.debug("TTS 播放中，跳过创建 STT 任务（保留 barge-in 检测）")
                else:
                    logger.debug("Utterance start: %.2fs", window.time_ns / 1e9)
                    if len(self._stt_tasks) >= self.max_stt_tasks:
                        logger.warning(
                            "Maximum number of STT tasks reached (%d), dropping window",
                            self.max_stt_tasks,
                        )
                    else:
                        self._window_queue = asyncio.Queue[SpeechWindow | None](
                            maxsize=self.window_queue_size
                        )
                        task = asyncio.create_task(
                            self._stt_utterance(self._window_queue)
                        )
                        task.add_done_callback(lambda t: self._stt_tasks.discard(t))
                        self._stt_tasks.add(task)

            if (
                not window.is_speech
                and last_speech is not None
                and (window.time_ns - last_speech) / 1e9 >= self.utterance_tail_seconds
            ):
                # 话语结束
                logger.debug("Utterance end: %.2fs", window.time_ns / 1e9)
                self._no_speech_event.set()
                last_speech = None
                utterance_start = None
                if self._window_queue is not None:
                    try:
                        self._window_queue.put_nowait(None)
                    except asyncio.QueueFull:
                        logger.warning(
                            "Frame queue is full, dropping middle frame for "
                            "utterance end"
                        )
                        self._window_queue.get_nowait()
                        self._window_queue.put_nowait(None)
                    self._window_queue = None

            # barge-in：即使 TTS 播放中（无 window_queue），持续语音也要清除
            # no_speech_event，让 SpeechController 抛出 SpeechInterruptedError 中断 TTS。
            # TTS 期间用更长的 barge_in_delay 过滤短促回声。
            if (
                utterance_start is not None
                and window.is_speech
                and self._no_speech_event.is_set()
            ):
                required_delay = (
                    self.barge_in_delay
                    if self.tts_active_event.is_set()
                    else self.no_speech_event_delay
                )
                if (window.time_ns - utterance_start) / 1e9 >= required_delay:
                    if self.tts_active_event.is_set():
                        logger.info("检测到 barge-in，准备中断 TTS")
                    self._no_speech_event.clear()

            if self._window_queue is not None:
                try:
                    self._window_queue.put_nowait(window)
                except asyncio.QueueFull:
                    dropped_windows += 1
                else:
                    if dropped_windows > 0:
                        logger.warning(
                            "Dropped %d audio windows due to full queue",
                            dropped_windows,
                        )
                    dropped_windows = 0

    async def _stt_utterance(self, queue: asyncio.Queue[SpeechWindow | None]) -> None:  # noqa: C901
        """处理语音窗口以进行转写。"""
        if self._transcript is None:
            msg = "Transcription controller not active"
            raise RuntimeError(msg)
        start: float | None = None
        end: float | None = None
        end_ts: float | None = None

        async def _window_iterator() -> AsyncIterator[SpeechWindow]:
            """从窗口队列产生语音窗口。"""
            nonlocal start, end, end_ts
            while True:
                window = await queue.get()
                if window is None:
                    end_ts = time.monotonic()
                    break
                if start is None:
                    start = window.time_ns / 1e9
                end = window.time_ns / 1e9 + calculate_audio_duration(
                    len(window.data), self.vad.audio_format
                )
                yield SpeechWindow(
                    data=convert_audio_format(
                        window.data, self.vad.audio_format, self.stt.audio_format
                    ),
                    time_ns=window.time_ns,
                    is_speech=window.is_speech,
                    speaker=window.speaker,
                )

        seg_count = 0
        try:
            stt_stream = self.stt.stream(_window_iterator())
            async for s in stt_stream:
                # TTS 播放期间丢弃 STT 结果，避免 Agent 声音回声被转写
                if self.tts_active_event.is_set():
                    logger.debug("Dropping STT segment during TTS playback: %s", s.text)
                    continue
                start = start or float("-inf")
                end = end or float("inf")
                segment_start = min(max(s.start, start), end)
                segment_end = max(min(s.end, end), segment_start)
                segment = TranscriptSegment(
                    text=s.text,
                    start=segment_start,
                    end=segment_end,
                    speaker=s.speaker,
                )
                self._transcript.add_segment(segment)
                self._notify("segment")
                logger.info(
                    "[%s] %s (%.2fs-%.2fs)",
                    segment.speaker if segment.speaker else "未知用户",
                    segment.text,
                    segment.start,
                    segment.end,
                )
                seg_count += 1
        except Exception:
            logger.exception("Error during STT processing")
            raise

        if seg_count > 0:
            if end_ts is not None:
                latency = time.monotonic() - end_ts
                log_level = logging.WARNING if latency > 0.3 else logging.DEBUG  # noqa: PLR2004
                logger.log(log_level, "STT utterance latency: %.3fs", latency)
            self._notify("utterance")
