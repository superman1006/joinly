import asyncio
import logging
import os
from collections.abc import AsyncIterator
from typing import Self

from google import genai
from google.genai import types

from joinly.core import TTS
from joinly.settings import get_settings
from joinly.types import AudioFormat
from joinly.utils.usage import add_usage

logger = logging.getLogger(__name__)

# BCP-47 语言代码到可用预置音色的映射。
# 用于为给定语言选择合理的默认音色。
# 音色列表见: https://ai.google.dev/gemini-api/docs/speech-generation#voice_options
DEFAULT_VOICES = {
    "en": "Zephyr",  # 英语 - 明亮
    "es": "Puck",  # 西班牙语 - 活泼
    "de": "Puck",  # 德语 - 活泼
    "fr": "Puck",  # 法语 - 活泼
    "it": "Puck",  # 意大利语 - 活泼
    "pt": "Puck",  # 葡萄牙语 - 活泼
    "ja": "Kore",  # 日语 - 稳重
    "ko": "Kore",  # 韩语 - 稳重
    "zh": "Kore",  # 中文 - 稳重
    "hi": "Puck",  # 印地语 - 活泼
    "ar": "Puck",  # 阿拉伯语 - 活泼
}

# 采样率常量
REQUIRED_SAMPLE_RATE = 24000


class GoogleTTS(TTS):
    """基于 Gemini 语音生成 API 的 TTS 服务。"""

    def __init__(
        self,
        *,
        model_name: str = "gemini-2.5-flash-preview-tts",
        voice_name: str | None = None,
        sample_rate: int = REQUIRED_SAMPLE_RATE,
        chunk_size_bytes: int = 4096,
    ) -> None:
        """初始化 Gemini TTS 服务。

        参数:
            model_name: 使用的 Gemini TTS 模型（默认 flash 预览版）。
            voice_name: 预置音色名（如 'Kore'、'Puck'、'Zephyr'）；若为 None，
                则按会话语言选择默认音色。
                全部 30 种音色见
                https://ai.google.dev/gemini-api/docs/speech-generation#voice_options
            sample_rate: 音频采样率。Gemini TTS 输出为 24kHz。
            chunk_size_bytes: 每次产出的音频块大小（字节）。
        """
        if os.getenv("GEMINI_API_KEY") is None and os.getenv("GOOGLE_API_KEY") is None:
            msg = "GEMINI_API_KEY or GOOGLE_API_KEY must be set in the environment."
            raise ValueError(msg)

        if sample_rate != REQUIRED_SAMPLE_RATE:
            logger.warning(
                "Gemini TTS outputs at %d Hz. Forcing sample_rate to %d.",
                REQUIRED_SAMPLE_RATE,
                REQUIRED_SAMPLE_RATE,
            )
            sample_rate = REQUIRED_SAMPLE_RATE

        self._model = model_name
        self._voice_name = voice_name or DEFAULT_VOICES.get(
            get_settings().language, "Puck"
        )
        self._chunk_size_bytes = chunk_size_bytes
        self._client: genai.Client | None = None
        self._lock = asyncio.Lock()

        # Gemini TTS 输出 24kHz、16 位 PCM 音频
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def __aenter__(self) -> Self:
        """初始化 Gemini 客户端。"""
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self._client = genai.Client(api_key=api_key)

        logger.info(
            "Initialized Gemini TTS with model: %s and voice: %s",
            self._model,
            self._voice_name,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """清理资源。"""
        self._client = None

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """将文本转为语音并流式输出音频数据。

        说明：Gemini TTS API 会一次性生成完整音频。
        本方法将完整音频分块以模拟流式输出。

        参数:
            text: 要转换为语音的文本。

        产生:
            bytes: PCM 音频数据块（24kHz，16 位）。
        """
        if self._client is None:
            msg = "TTS service is not initialized."
            raise RuntimeError(msg)

        async with self._lock:
            logger.debug("Generating audio for text: '%s'", text)

            try:
                # 配置语音生成
                config = types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=self._voice_name,
                            )
                        )
                    ),
                )

                # 生成音频
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=text,
                    config=config,
                )

                # 提取音频数据
                def _validate_response() -> None:
                    """校验响应中是否包含音频数据。"""
                    if (
                        not response.candidates
                        or not response.candidates[0].content
                        or not response.candidates[0].content.parts
                        or not response.candidates[0].content.parts[0].inline_data
                    ):
                        msg = "No audio data in response"
                        raise RuntimeError(msg)  # noqa: TRY301

                _validate_response()

                # 类型收窄：校验保证此处非 None
                if not response.candidates:
                    msg = "No candidates after validation"
                    raise RuntimeError(msg)  # noqa: TRY301

                candidate = response.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    msg = "No content or parts after validation"
                    raise RuntimeError(msg)  # noqa: TRY301

                part = candidate.content.parts[0]
                if not part.inline_data or not part.inline_data.data:
                    msg = "No inline data after validation"
                    raise RuntimeError(msg)  # noqa: TRY301

                audio_data = part.inline_data.data

                if not audio_data:
                    logger.warning("Received empty audio data from Gemini TTS.")
                    return

                # 记录用量
                add_usage(
                    service="gemini_tts",
                    usage={"characters": len(text)},
                    meta={"model": self._model, "voice": self._voice_name},
                )

                logger.debug("Generated %d bytes of audio data.", len(audio_data))

                # 分块音频数据以便流式输出
                for i in range(0, len(audio_data), self._chunk_size_bytes):
                    yield audio_data[i : i + self._chunk_size_bytes]

            except Exception as e:
                logger.exception("Error during Gemini TTS generation")
                msg = f"Failed to generate audio from Gemini TTS: {e}"
                raise RuntimeError(msg) from e
