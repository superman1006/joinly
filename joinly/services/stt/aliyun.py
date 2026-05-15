import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Self
from urllib.parse import quote

import aiohttp

from joinly.core import STT
from joinly.types import AudioFormat, SpeechWindow, TranscriptSegment

logger = logging.getLogger(__name__)

_NLS_GATEWAY = "wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1"
_TOKEN_URL = "https://nls-meta.cn-shanghai.aliyuncs.com/"


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
    # 手动拼接，避免 urlencode 对已编码字符二次编码
    canonicalized = "&".join(
        f"{_penc(k)}={_penc(v)}" for k, v in sorted(params.items())
    )
    string_to_sign = f"GET&{_penc('/')}&{_penc(canonicalized)}"
    key = (access_key_secret + "&").encode()
    sig = hmac.new(key, string_to_sign.encode(), hashlib.sha1).digest()
    params["Signature"] = base64.b64encode(sig).decode()
    return params


class AliyunSTT(STT):
    """使用阿里云实时语音识别（NLS）将音频转写为文字。"""

    def __init__(self, *, sample_rate: int = 16000) -> None:
        self._access_key_id = os.environ["ALIYUN_ACCESS_KEY_ID"]
        self._access_key_secret = os.environ["ALIYUN_ACCESS_KEY_SECRET"]
        self._app_key = os.environ["ALIYUN_NLS_APP_KEY"]
        self._sample_rate = sample_rate
        self._token: str | None = None
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=2)

    async def __aenter__(self) -> Self:
        params = _sign_rpc(self._access_key_id, self._access_key_secret)
        async with aiohttp.ClientSession() as session:
            async with session.get(_TOKEN_URL, params=params) as resp:
                data = await resp.json(content_type=None)
        token_info = data.get("Token", {})
        self._token = token_info.get("Id")
        if not self._token:
            msg = f"获取阿里云令牌失败: {data}"
            raise RuntimeError(msg)
        logger.info("阿里云 STT 令牌获取成功，有效期至 %s", token_info.get("ExpireTime"))
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._token = None

    async def stream(
        self, windows: AsyncIterator[SpeechWindow]
    ) -> AsyncIterator[TranscriptSegment]:
        """流式处理音频并产生转写片段。"""
        if self._token is None:
            msg = "阿里云 STT 服务未启动"
            raise RuntimeError(msg)

        task_id = uuid.uuid4().hex
        url = f"{_NLS_GATEWAY}?token={self._token}"

        stream_start: float | None = None
        result_queue: asyncio.Queue[TranscriptSegment | None] = asyncio.Queue()

        def _make_msg(name: str, extra: dict[str, Any] | None = None) -> str:
            header: dict[str, Any] = {
                "message_id": uuid.uuid4().hex,
                "task_id": task_id,
                "namespace": "SpeechTranscriber",
                "name": name,
                "appkey": self._app_key,
            }
            msg: dict[str, Any] = {"header": header}
            if extra:
                msg["payload"] = extra
            return json.dumps(msg)

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                # 发送 StartTranscription
                await ws.send_str(
                    _make_msg(
                        "StartTranscription",
                        {
                            "format": "pcm",
                            "sample_rate": self._sample_rate,
                            "enable_intermediate_result": False,
                            "enable_punctuation_prediction": True,
                            "enable_inverse_text_normalization": True,
                        },
                    )
                )

                # 等待 TranscriptionStarted 确认
                started = False
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        name = data.get("header", {}).get("name", "")
                        if name == "TranscriptionStarted":
                            started = True
                            logger.info("阿里云 NLS 转写已启动")
                            break
                        if name == "TaskFailed":
                            err = data.get("header", {}).get("status_message", "")
                            msg_text = f"阿里云 NLS 启动失败: {err}"
                            raise RuntimeError(msg_text)

                if not started:
                    msg_text = "阿里云 NLS 未收到 TranscriptionStarted 确认"
                    raise RuntimeError(msg_text)

                async def _recv_loop() -> None:
                    async for ws_msg in ws:
                        if ws_msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(ws_msg.data)
                            name = data.get("header", {}).get("name", "")
                            if name == "SentenceEnd":
                                payload = data.get("payload", {})
                                text = payload.get("result", "")
                                if text:
                                    begin_ms: int = payload.get("begin_time", 0)
                                    end_ms: int = payload.get("time", 0)
                                    offset = stream_start or 0.0
                                    seg = TranscriptSegment(
                                        text=text,
                                        start=offset + begin_ms / 1000,
                                        end=offset + end_ms / 1000,
                                    )
                                    await result_queue.put(seg)
                            elif name in ("TranscriptionCompleted", "TaskFailed"):
                                if name == "TaskFailed":
                                    err = data.get("header", {}).get("status_message", "")
                                    logger.error("阿里云 NLS 错误: %s", err)
                                await result_queue.put(None)
                                return
                        elif ws_msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            await result_queue.put(None)
                            return

                recv_task = asyncio.create_task(_recv_loop())

                try:
                    async for window in windows:
                        if stream_start is None:
                            stream_start = window.time_ns / 1e9
                        await ws.send_bytes(window.data)

                    # 发送 StopTranscription
                    await ws.send_str(_make_msg("StopTranscription"))

                    while True:
                        try:
                            segment = await asyncio.wait_for(
                                result_queue.get(), timeout=15.0
                            )
                        except TimeoutError:
                            logger.warning("阿里云 STT 等待结果超时")
                            break
                        if segment is None:
                            break
                        yield segment
                finally:
                    recv_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await recv_task
