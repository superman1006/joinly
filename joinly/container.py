"""依赖注入容器（SessionContainer）。

负责根据 ``Settings`` 中的短令牌解析并实例化所有组件，通过 ``AsyncExitStack``
统一管理异步上下文（模型加载、WebSocket 连接等）的生命周期。

解析约定（``_resolve``）::
    令牌 ``aliyun`` + 后缀 ``STT`` → ``joinly.services.stt.aliyun.AliyunSTT``
    令牌 ``browser`` + 后缀 ``MeetingProvider`` → ``joinly.providers.browser...``

组装顺序: VAD → STT → TTS → MeetingProvider → 控制器，再将 reader/writer 与
VAD/STT/TTS 注入控制器，最后构造 ``MeetingSession``。
"""

import importlib
import logging
import re
from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    AsyncExitStack,
)
from typing import Any, TypeVar

from joinly.session import MeetingSession
from joinly.settings import Settings, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _describe(instance: object, args: dict[str, Any]) -> str:
    """提取实例的关键参数（model/voice 等）用于日志展示。"""
    keys = ("model", "model_name", "_model", "_model_name", "voice", "_voice")
    details: list[str] = []
    for k in ("model", "voice"):
        if k in args:
            details.append(f"{k}={args[k]}")
    if not details:
        for k in keys:
            v = getattr(instance, k, None)
            if isinstance(v, str) and v:
                details.append(f"{k.lstrip('_')}={v}")
                break
    return f"{type(instance).__name__}" + (f"({', '.join(details)})" if details else "")


def _resolve(spec: str | type[T], *, base: str, suffix: str) -> type[T]:
    """将具体类型、点分路径或短令牌解析为类对象。

    按约定发现路径：
    - 若 `spec` 为类型，直接返回。
    - 若 `spec` 为点分路径，导入模块并返回该类。
    - 若 `spec` 为短令牌，映射为
        `<base>.<token_lowercase>.<token_camelcase><suffix>`。
    """
    if isinstance(spec, type):
        return spec

    if "." in spec:
        # 完全限定名
        mod, _, cls = spec.rpartition(".")
    else:
        # 短令牌
        if spec.lower().endswith(suffix.lower()):
            base_name = spec[: -len(suffix)]
        else:
            base_name = "".join(p.capitalize() for p in re.split(r"[_\- ]+", spec))
        mod = f"{base}.{base_name.lower()}"
        cls = base_name + suffix

    try:
        module = importlib.import_module(mod)
    except ModuleNotFoundError as e:
        if e.name == mod:
            msg = f"Module '{mod}' not found."
        else:
            msg = (
                f"Missing dependency '{e.name}' when importing module '{mod}'. "
                "You may need to install optional dependencies for this component."
            )
        raise ImportError(msg) from e

    try:
        return getattr(module, cls)
    except AttributeError as e:
        msg = f"Cannot resolve class '{cls}' in module '{mod}'"
        raise ImportError(msg) from e


class SessionContainer:
    """会议会话的依赖注入容器。

    作为异步上下文管理器使用::

        async with SessionContainer() as meeting_session:
            await meeting_session.join_meeting(url)
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """初始化会话容器。

        参数:
            settings: 可选配置；默认使用 ContextVar 中的 ``get_settings()``。
        """
        self._settings = settings or get_settings()
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> MeetingSession:
        """进入上下文管理器并创建会议会话。"""
        try:
            vad = await self._build(
                self._settings.vad,
                "joinly.services.vad",
                "VAD",
                self._settings.vad_args,
            )
            stt_extra_args = (
                {
                    "finalize_silence": max(
                        0.1,
                        float(
                            self._settings.transcription_controller_args.get(
                                "utterance_tail_seconds",
                                0.6,
                            )
                        )
                        - 0.225,
                    )
                }
                if _resolve(
                    self._settings.stt,
                    base="joinly.services.stt",
                    suffix="STT",
                ).__name__
                == "DeepgramSTT"
                else {}
            )
            stt = await self._build(
                self._settings.stt,
                "joinly.services.stt",
                "STT",
                stt_extra_args | self._settings.stt_args,
            )
            tts = await self._build(
                self._settings.tts,
                "joinly.services.tts",
                "TTS",
                self._settings.tts_args,
            )

            logger.info(
                "已加载 STT: %s | TTS: %s | VAD: %s",
                _describe(stt, self._settings.stt_args),
                _describe(tts, self._settings.tts_args),
                type(vad).__name__,
            )

            provider_extra_args = (
                {
                    "reader_byte_depth": vad.audio_format.byte_depth,
                    "writer_byte_depth": tts.audio_format.byte_depth,
                }
                if _resolve(
                    self._settings.meeting_provider,
                    base="joinly.providers",
                    suffix="MeetingProvider",
                ).__name__
                == "BrowserMeetingProvider"
                else {}
            )
            meeting_provider = await self._build(
                self._settings.meeting_provider,
                "joinly.providers",
                "MeetingProvider",
                provider_extra_args | self._settings.meeting_provider_args,
            )

            transcription_controller = await self._build(
                self._settings.transcription_controller,
                "joinly.controllers.transcription",
                "TranscriptionController",
                self._settings.transcription_controller_args,
            )
            speech_controller = await self._build(
                self._settings.speech_controller,
                "joinly.controllers.speech",
                "SpeechController",
                self._settings.speech_controller_args,
            )

            transcription_controller.reader = meeting_provider.audio_reader
            transcription_controller.vad = vad
            transcription_controller.stt = stt

            speech_controller.writer = meeting_provider.audio_writer
            speech_controller.tts = tts
            speech_controller.no_speech_event = transcription_controller.no_speech_event
            speech_controller.tts_active_event = (
                transcription_controller.tts_active_event
            )

            meeting_session = MeetingSession(
                meeting_provider=meeting_provider,
                transcription_controller=transcription_controller,
                speech_controller=speech_controller,
                video_reader=meeting_provider.video_reader,
            )
        except:
            await self._stack.aclose()
            raise

        return meeting_session

    async def __aexit__(self, *_exc: object) -> None:
        """退出上下文并清理资源。"""
        await self._stack.aclose()

    async def _build(
        self, spec: str | type[T], base: str, suffix: str, args: dict[str, Any]
    ) -> T:
        """构建指定类的实例。"""
        cls = _resolve(spec, base=base, suffix=suffix)
        instance = cls(**args)
        if isinstance(instance, AbstractAsyncContextManager):
            return await self._stack.enter_async_context(instance)
        if isinstance(instance, AbstractContextManager):
            return self._stack.enter_context(instance)
        return instance
