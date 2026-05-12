import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncIterator

from elevenlabs.client import AsyncElevenLabs

from joinly.core import TTS
from joinly.settings import get_settings
from joinly.types import AudioFormat
from joinly.utils.usage import add_usage

logger = logging.getLogger(__name__)

DEFAULT_VOICES = defaultdict(
    lambda: "XrExE9yKIg1WjnnlVkGX",
    {
        "de": "1iF3vHdwHKuVKSPDK23Z",
        "en": "XrExE9yKIg1WjnnlVkGX",
    },
)


class ElevenlabsTTS(TTS):
    """将文本转为语音的 TTS 服务。"""

    def __init__(
        self,
        *,
        voice_id: str | None = None,
        model_id: str = "eleven_flash_v2_5",
        sample_rate: int = 24000,
    ) -> None:
        """初始化 TTS 服务。

        参数:
            voice_id: ElevenLabs 音色 ID。
            model_id: ElevenLabs 模型 ID（默认 "eleven_flash_v2_5"）。
            sample_rate: 音频采样率（默认 24000）。
        """
        self._voice_id = voice_id or DEFAULT_VOICES[get_settings().language]
        self._model_id = model_id
        self._output_format = f"pcm_{sample_rate}"
        self._client = AsyncElevenLabs()
        self._lock = asyncio.Lock()
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    def stream(self, text: str) -> AsyncIterator[bytes]:
        """将文本转为语音并流式输出音频数据。

        参数:
            text: 要转换为语音的文本。

        返回:
            AsyncIterator[bytes]: 异步迭代器，逐块产出音频数据。
        """
        language_code = None
        if self._model_id in ("eleven_flash_v2_5", "eleven_turbo_v2_5"):
            language_code = get_settings().language

        add_usage(
            service="elevenlabs_tts",
            usage={"characters": len(text)},
            meta={"model": self._model_id, "voice": self._voice_id},
        )

        return self._client.text_to_speech.stream(
            text=text,
            voice_id=self._voice_id,
            model_id=self._model_id,
            output_format=self._output_format,
            language_code=language_code,
        )
