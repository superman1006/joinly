import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Self

from deepgram import (
    AsyncListenWebSocketClient,
    DeepgramClient,
    DeepgramClientOptions,
    LiveOptions,
    LiveResultResponse,
    LiveTranscriptionEvents,
)

from joinly.core import STT
from joinly.settings import get_settings
from joinly.types import (
    AudioFormat,
    SpeechWindow,
    TranscriptSegment,
)
from joinly.utils.audio import calculate_audio_duration
from joinly.utils.logging import LOGGING_TRACE
from joinly.utils.usage import add_usage

logger = logging.getLogger(__name__)


class DeepgramSTT(STT):
    """使用 Deepgram 将音频转写为文字的类。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        model_name: str | None = None,
        sample_rate: int = 16000,
        hotwords: list[str] | None = None,
        finalize_silence: float = 0.375,
        finalize_min_speech: float = 0.03,
        padding_silence: float = 0.1,
        stream_idle_timeout: float = 1.0,
        mip_opt_out: bool = True,
    ) -> None:
        """初始化 DeepgramSTT。

        参数:
            model_name: Deepgram 模型名（默认：支持语言用 "nova-3-general"，否则
                "nova-2-general"）。
            sample_rate: 音频采样率（默认 16000）。
            hotwords: 用于提升转写准确率的热词列表。
            finalize_silence: 判定流结束前等待的静音时长（默认 0.375 秒）。
            finalize_min_speech: 视为语音的最短时长（默认 0.03 秒）。
            padding_silence: 每个音频窗口开头填充的静音时长（默认 0.1 秒）。
            stream_idle_timeout: finalize 后等待关闭流的时长（默认 1.0 秒）；正常
                finalize 后不应触发。
            mip_opt_out: 是否退出模型改进计划（默认 True）。详见
                https://developers.deepgram.com/docs/the-deepgram-model-improvement-partnership-program
        """
        config = DeepgramClientOptions(options={"keep_alive": True})
        dg = DeepgramClient(config=config)
        self._client: AsyncListenWebSocketClient = dg.listen.asyncwebsocket.v("1")  # type: ignore[attr-type]
        self.model_name = model_name or (
            "nova-3-general"
            if get_settings().language in ["en", "de", "nl", "sv", "da"]
            else "nova-2-general"
        )
        self.finalize_silence = float(finalize_silence)
        self.finalize_min_speech = float(finalize_min_speech)
        self._live_options = LiveOptions(
            model=self.model_name,
            encoding="linear16",
            sample_rate=sample_rate,
            language=get_settings().language,
            channels=1,
            endpointing=False,
            interim_results=False,
            punctuate=True,
            profanity_filter=True,
            vad_events=False,
            keyterm=(
                (hotwords or []) + [get_settings().name]
                if self.model_name.startswith("nova-3")
                else None
            ),
        )
        self._mip_opt_out = bool(mip_opt_out)
        self._stream_idle_timeout = stream_idle_timeout
        self._sent_seconds = 0.0
        self._queue: asyncio.Queue[TranscriptSegment | None] | None = None
        self._lock = asyncio.Lock()
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)
        self._padding_silence_dur = float(padding_silence)
        self._padding_silence = b"\x00" * (
            int(self._padding_silence_dur * self.audio_format.sample_rate)
            * self.audio_format.byte_depth
        )

    async def __aenter__(self) -> Self:
        """进入上下文。"""
        if await self._client.is_connected():
            msg = "Already started the audio stream."
            raise RuntimeError(msg)

        self._sent_seconds = 0.0
        self._queue = asyncio.Queue[TranscriptSegment | None]()

        async def on_result(
            _client: AsyncListenWebSocketClient,
            result: LiveResultResponse,
            **_kwargs: object,
        ) -> None:
            """处理 WebSocket 收到的消息。"""
            logger.log(LOGGING_TRACE, "Received message: %s", result)
            if result.channel.alternatives:
                transcript = result.channel.alternatives[0].transcript
                if transcript:
                    segment = TranscriptSegment(
                        text=transcript,
                        start=result.start - self._sent_seconds,
                        end=result.start - self._sent_seconds + result.duration,
                    )
                    await self._queue.put(segment)  # type: ignore[attr-defined]
            if result.from_finalize:
                await self._queue.put(None)  # type: ignore[attr-defined]

        self._client.on(LiveTranscriptionEvents.Transcript, on_result)  # type: ignore[arg-type]

        logger.info(
            "Connecting to Deepgram STT service with model: %s",
            self._live_options.model,
        )
        await self._client.start(
            self._live_options, addons={"mip_opt_out": self._mip_opt_out}
        )
        if not await self._client.is_connected():
            msg = "Failed to connect to Deepgram STT service."
            logger.error(msg)
            raise RuntimeError(msg)
        logger.debug("Connected to Deepgram STT service")

        return self

    async def __aexit__(self, *_exc: object) -> None:
        """退出上下文。"""
        logger.debug("Closing Deepgram STT service connection")
        await self._client.finish()
        self._queue = None

    async def stream(  # noqa: C901, PLR0915
        self, windows: AsyncIterator[SpeechWindow]
    ) -> AsyncIterator[TranscriptSegment]:
        """流式处理音频窗口并产生转写片段。

        参数:
            windows: 语音窗口的异步迭代器。

        产生:
            TranscriptSegment: 转写得到的片段。
        """
        if self._queue is None or not await self._client.is_connected():
            msg = "STT service is not started."
            raise RuntimeError(msg)

        stream_start: float | None = None
        stream_end: float | None = None
        speaker_windows: list[tuple[float, float, str]] = []
        finalize_pending: int = 0

        async def _producer() -> None:
            """负责发送音频数据的生产者协程。"""
            nonlocal stream_start, stream_end, finalize_pending
            if self._padding_silence:
                self._sent_seconds += self._padding_silence_dur
                await self._client.send(self._padding_silence)
                add_usage(
                    service="deepgram_stt",
                    usage={"minutes": self._padding_silence_dur / 60},
                    meta={"model": self.model_name, "mip_opt_out": self._mip_opt_out},
                )

            silence_dur: float = 0.0
            speech_dur: float = 0.0
            async for window in windows:
                if stream_start is None:
                    stream_start = window.time_ns / 1e9
                cur = window.time_ns / 1e9
                dur = calculate_audio_duration(len(window.data), self.audio_format)
                stream_end = cur + dur
                if window.speaker is not None:
                    speaker_windows.append(
                        (cur - stream_start, cur - stream_start + dur, window.speaker)
                    )
                await self._client.send(window.data)
                add_usage(
                    service="deepgram_stt",
                    usage={"minutes": dur / 60},
                    meta={"model": self.model_name, "mip_opt_out": self._mip_opt_out},
                )

                if window.is_speech:
                    silence_dur = 0.0
                    speech_dur += dur
                else:
                    silence_dur += dur
                    if (
                        silence_dur >= self.finalize_silence
                        and speech_dur >= self.finalize_min_speech
                    ):
                        logger.debug(
                            "Finalizing stream after %.2fs of silence "
                            "with %.2fs of speech.",
                            silence_dur,
                            speech_dur,
                        )
                        finalize_pending += 1
                        await self._client.finalize()
                        silence_dur = 0.0
                        speech_dur = 0.0

            if speech_dur >= self.finalize_min_speech:
                finalize_pending += 1
                await self._client.finalize()

            # 在不发送数据的情况下增大 finalize 以触发下一轮循环
            finalize_pending += 1
            await self._queue.put(None)  # type: ignore[attr-defined]

        async with self._lock:
            while not self._queue.empty():
                _ = self._queue.get_nowait()
            producer = asyncio.create_task(_producer())

            try:
                while True:
                    cm = (
                        asyncio.timeout(self._stream_idle_timeout)
                        if producer.done()
                        else contextlib.nullcontext()
                    )
                    try:
                        async with cm:
                            segment = await self._queue.get()
                    except TimeoutError:
                        logger.warning(
                            "Stream idle timeout (%.2fs) reached before reaching "
                            "finalization. Terminating stream.",
                            self._stream_idle_timeout,
                        )
                        break
                    if segment is None:
                        finalize_pending -= 1
                        if producer.done() and finalize_pending <= 0:
                            break
                        continue

                    speakers: defaultdict[str, float] = defaultdict(float)
                    for start, end, speaker in speaker_windows:
                        speakers[speaker] += max(
                            0.0, min(end, segment.end) - max(start, segment.start)
                        )
                    speaker, speaker_time = max(
                        speakers.items(),
                        key=lambda x: x[1],
                        default=(None, 0),
                    )
                    if speaker_time < 0.1 * (segment.end - segment.start):
                        speaker = None

                    yield TranscriptSegment(
                        text=segment.text,
                        start=segment.start + (stream_start or 0),
                        end=segment.end + (stream_start or 0),
                        speaker=speaker,
                    )
            finally:
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer
                self._sent_seconds += (stream_end or 0) - (stream_start or 0)
