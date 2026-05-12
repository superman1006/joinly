import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Self

from deepgram import (
    AsyncSpeakWebSocketClient,
    DeepgramClient,
    DeepgramClientOptions,
    SpeakWebSocketEvents,
    SpeakWSOptions,
)

from joinly.core import TTS
from joinly.settings import get_settings
from joinly.types import AudioFormat
from joinly.utils.usage import add_usage

logger = logging.getLogger(__name__)


class DeepgramTTS(TTS):
    """将文本转为语音的 TTS 服务。"""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        sample_rate: int = 24000,
        mip_opt_out: bool = True,
    ) -> None:
        """初始化 TTS 服务。

        参数:
            model_name: 使用的 Deepgram TTS 模型（英语默认 "aura-2-andromeda-en"，
                西班牙语默认 "aura-2-estrella-es"）。
            sample_rate: 音频采样率（默认 24000）。
            mip_opt_out: 是否退出模型改进计划（默认 True）。详见
                https://developers.deepgram.com/docs/the-deepgram-model-improvement-partnership-program
        """
        config = DeepgramClientOptions(
            options={
                "keep_alive": True,
                "speaker_playback": False,
            }
        )
        dg = DeepgramClient(config=config)
        self._client: AsyncSpeakWebSocketClient = dg.speak.asyncwebsocket.v("1")
        if model_name is None and get_settings().language not in ["en", "es"]:
            logger.warning(
                "Unsupported language %s for Deepgram TTS, falling back to English.",
                get_settings().language,
            )
        self.model_name = model_name or (
            "aura-2-estrella-es"
            if get_settings().language == "es"
            else "aura-2-andromeda-en"
        )
        self._speak_options = SpeakWSOptions(
            model=self.model_name,
            encoding="linear16",
            sample_rate=sample_rate,
        )
        self._mip_opt_out = bool(mip_opt_out)
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._lock = asyncio.Lock()
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def __aenter__(self) -> Self:
        """进入异步上下文管理器。"""
        if await self._client.is_connected():
            msg = "Already started the audio stream."
            raise RuntimeError(msg)

        self._queue = asyncio.Queue[bytes | None]()

        async def on_data(
            _client: AsyncSpeakWebSocketClient, data: bytes, **_kwargs: object
        ) -> None:
            """处理 WebSocket 收到的二进制数据。"""
            logger.debug("Received binary data of size: %s", len(data))
            await self._queue.put(data)  # type: ignore[attr-defined]

        async def on_flushed(
            _client: AsyncSpeakWebSocketClient, **_kwargs: object
        ) -> None:
            """处理 WebSocket 的 flushed 事件。"""
            logger.debug("Flushed event received.")
            await self._queue.put(None)  # type: ignore[attr-defined]

        self._client.on(SpeakWebSocketEvents.AudioData, on_data)  # type: ignore[arg-type]
        self._client.on(SpeakWebSocketEvents.Flushed, on_flushed)  # type: ignore[arg-type]

        logger.info(
            "Connecting to Deepgram TTS service with model: %s",
            self._speak_options.model,
        )
        await self._client.start(
            self._speak_options, addons={"mip_opt_out": self._mip_opt_out}
        )
        if not await self._client.is_connected():
            msg = "Failed to connect to Deepgram TTS service."
            logger.error(msg)
            raise RuntimeError(msg)
        logger.debug("Connected to Deepgram TTS service")

        return self

    async def __aexit__(self, *_exc: object) -> None:
        """退出异步上下文管理器。"""
        logger.debug("Closing Deepgram TTS service connection")
        await self._client.finish()
        self._queue = None

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """将文本转为语音并流式输出音频数据。

        参数:
            text: 要转换为语音的文本。

        产生:
            bytes: 音频数据。
        """
        if self._queue is None or not await self._client.is_connected():
            msg = "TTS service is not started."
            raise RuntimeError(msg)

        async with self._lock:
            # 排空队列，避免残留旧数据
            while not self._queue.empty():
                _ = self._queue.get_nowait()

            try:
                await self._client.send_text(text)
                await self._client.flush()
                add_usage(
                    service="deepgram_tts",
                    usage={"characters": len(text)},
                    meta={"model": self.model_name, "mip_opt_out": self._mip_opt_out},
                )

                while (chunk := await self._queue.get()) is not None:
                    yield chunk
            finally:
                await self._client.clear()
