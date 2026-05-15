import base64
import hashlib
import hmac
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Self
from urllib.parse import quote

import aiohttp

from joinly.core import TTS
from joinly.settings import get_settings
from joinly.types import AudioFormat
from joinly.utils.usage import add_usage

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://nls-meta.cn-shanghai.aliyuncs.com/"  # noqa: S105
_TTS_URL = "https://nls-gateway.cn-shanghai.aliyuncs.com/stream/v1/tts"

# 默认音色：aixia 是阿里云的高质量中文女声，支持 24000 Hz
# 备选：sicheng（男声）、sijia（女声）、aiqi（女童声）
DEFAULT_VOICES = {
    "zh": "aixia",
    "en": "aixia",
}

# 每次从 HTTP 响应读取的音频块大小
_CHUNK_SIZE = 4096


def _penc(s: str) -> str:
    """RFC 3986 百分号编码（Aliyun V1 签名专用）。"""
    return quote(s, safe="~")


def _sign_rpc(access_key_id: str, access_key_secret: str) -> dict[str, str]:
    """构造阿里云 RPC V1 签名请求参数，用于获取 NLS 令牌。"""
    params: dict[str, str] = {
        "Action": "CreateToken",
        "Version": "2019-02-28",
        "Format": "JSON",
        "AccessKeyId": access_key_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    canonicalized = "&".join(
        f"{_penc(k)}={_penc(v)}" for k, v in sorted(params.items())
    )
    string_to_sign = f"GET&{_penc('/')}&{_penc(canonicalized)}"
    key = (access_key_secret + "&").encode()
    sig = hmac.new(key, string_to_sign.encode(), hashlib.sha1).digest()
    params["Signature"] = base64.b64encode(sig).decode()
    return params


class AliyunTTS(TTS):
    """使用阿里云语音合成 REST 接口（一次性合成）将文本转为 PCM 音频。"""

    def __init__(
        self,
        *,
        voice: str | None = None,
        sample_rate: int = 24000,
        volume: int = 50,
        speech_rate: int = 0,
        pitch_rate: int = 0,
    ) -> None:
        """初始化阿里云 TTS。

        参数:
            voice: 发音人（默认 aixia，支持 24000 Hz 高质量音色）。
                可选 sicheng（男）/sijia（女）/aiqi（女童）等。
                注意 xiaoyun 等基础音色仅支持 16000 Hz，与下游虚拟麦克风
                的 24000 Hz 不匹配；longxiaochun 等大模型音色仅支持流式接口。
            sample_rate: 音频采样率（默认 24000 Hz，匹配虚拟麦克风）。
            volume: 音量，0～100（默认 50）。
            speech_rate: 语速，-500～500（默认 0）。
            pitch_rate: 语调，-500～500（默认 0）。
        """
        self._access_key_id = os.environ["ALIYUN_ACCESS_KEY_ID"]
        self._access_key_secret = os.environ["ALIYUN_ACCESS_KEY_SECRET"]
        self._app_key = os.environ["ALIYUN_NLS_APP_KEY"]
        self._voice = voice or DEFAULT_VOICES.get(
            get_settings().language, DEFAULT_VOICES["zh"]
        )
        self._sample_rate = sample_rate
        self._volume = volume
        self._speech_rate = speech_rate
        self._pitch_rate = pitch_rate
        self._token: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def __aenter__(self) -> Self:
        """获取 NLS 访问令牌并打开 HTTP 会话。"""
        params = _sign_rpc(self._access_key_id, self._access_key_secret)
        async with (
            aiohttp.ClientSession() as session,
            session.get(_TOKEN_URL, params=params) as resp,
        ):
            data = await resp.json(content_type=None)
        token_info = data.get("Token", {})
        self._token = token_info.get("Id")
        if not self._token:
            msg = f"获取阿里云令牌失败: {data}"
            raise RuntimeError(msg)
        self._session = aiohttp.ClientSession()
        logger.info(
            "阿里云 TTS 令牌获取成功，有效期至 %s，音色 %s",
            token_info.get("ExpireTime"),
            self._voice,
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """释放令牌并关闭 HTTP 会话。"""
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._token = None

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """将文本转为语音并流式输出 PCM 音频数据。"""
        if self._token is None or self._session is None:
            msg = "阿里云 TTS 服务未启动"
            raise RuntimeError(msg)

        # 阿里云 REST TTS 要求 token 通过 query 参数传递
        # （body 里也可以放 token，但 query 是官方文档推荐方式）
        body = {
            "appkey": self._app_key,
            "token": self._token,
            "text": text,
            "format": "pcm",
            "sample_rate": self._sample_rate,
            "voice": self._voice,
            "volume": self._volume,
            "speech_rate": self._speech_rate,
            "pitch_rate": self._pitch_rate,
        }

        logger.debug(
            "阿里云 TTS 请求: voice=%s sample_rate=%s text_len=%d",
            self._voice,
            self._sample_rate,
            len(text),
        )

        async with self._session.post(
            _TTS_URL,
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-NLS-Token": self._token,
            },
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            task_id = resp.headers.get("X-NLS-RequestId", "")

            # 成功时返回原始音频（Content-Type: audio/mpeg），失败返回 JSON
            if resp.status != 200 or "application/json" in content_type:  # noqa: PLR2004
                try:
                    err_payload = await resp.json(content_type=None)
                except Exception:
                    err_payload = await resp.text()
                msg_text = (
                    f"阿里云 TTS 合成失败 (HTTP {resp.status}, "
                    f"request_id={task_id}, content_type={content_type}): "
                    f"{err_payload}"
                )
                raise RuntimeError(msg_text)

            add_usage(
                service="aliyun_tts",
                usage={"characters": len(text)},
                meta={"voice": self._voice, "sample_rate": self._sample_rate},
            )

            total = 0
            async for chunk in resp.content.iter_chunked(_CHUNK_SIZE):
                if chunk:
                    total += len(chunk)
                    yield chunk

            if total == 0:
                msg_text = (
                    f"阿里云 TTS 返回空音频 (request_id={task_id}, "
                    f"content_type={content_type})"
                )
                raise RuntimeError(msg_text)
            logger.debug("阿里云 TTS 合成完成，共 %d 字节", total)
