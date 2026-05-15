"""joinly 核心协议定义（Protocol）。

本模块是 joinly 的架构契约层，所有可替换组件（VAD、STT、TTS、会议提供方、控制器）
均通过此处定义的 Protocol 解耦。`SessionContainer` 按短令牌（如 ``"whisper"``、
``"aliyun"``）动态解析具体实现类。

数据流概览::

    会议音频 → AudioReader → VAD → STT → TranscriptionController → Transcript
    Agent 文本 → SpeechController → TTS → AudioWriter → 会议麦克风

主要 Protocol:
    - ``AudioReader`` / ``AudioWriter``: 会议侧 PCM 音频 I/O
    - ``VAD``: 语音活动检测，输出带 is_speech 标记的窗口
    - ``STT`` / ``TTS``: 语音转写与合成
    - ``MeetingProvider``: 加入/离开会议、聊天、静音等平台操作
    - ``TranscriptionController`` / ``SpeechController``: 转写与朗读流程编排
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from joinly.types import (
    ActionAnimation,
    AudioChunk,
    AudioFormat,
    MeetingChatHistory,
    MeetingParticipant,
    SpeechWindow,
    Transcript,
    TranscriptSegment,
    UIUpdate,
    VideoSnapshot,
)
from joinly.utils.clock import Clock
from joinly.utils.events import EventBus


class AudioReader(Protocol):
    """音频流数据源的协议。

    定义提供音频数据的对象的接口。

    属性:
        audio_format (AudioFormat): 正在读取的音频数据格式。
    """

    audio_format: AudioFormat

    async def read(self) -> AudioChunk:
        """读取一块音频数据。

        返回:
            AudioChunk: 一块音频数据。
        """
        ...


class AudioWriter(Protocol):
    """音频输出目标的协议。

    定义消费音频数据的对象的接口。

    属性:
        audio_format (AudioFormat): 正在写入的音频数据格式。
        chunk_size (int): 可接受的最小音频块大小（字节）。
    """

    audio_format: AudioFormat
    chunk_size: int

    async def write(self, data: bytes) -> None:
        """将音频数据写入输出端。

        参数:
            data: 原始 PCM 音频数据。
        """
        ...


class VideoReader(Protocol):
    """视频流数据源的协议。

    定义提供视频数据的对象的接口。
    """

    async def snapshot(self) -> VideoSnapshot:
        """捕获当前视频帧的快照。

        返回:
            VideoSnapshot: 当前视频帧的快照。
        """
        ...


class VAD(Protocol):
    """语音活动检测（VAD）的协议。

    定义在音频流中检测语音的接口。

    属性:
        audio_format (AudioFormat): VAD 处理所期望的音频数据格式。
    """

    audio_format: AudioFormat

    def stream(self, chunks: AsyncIterator[AudioChunk]) -> AsyncIterator[SpeechWindow]:
        """在音频窗口上流式输出语音活动检测结果。

        参数:
            chunks: 提供音频块的异步迭代器。

        返回:
            AsyncIterator[SpeechWindow]: 包含语音信息的音频窗口流。
        """
        ...


class STT(Protocol):
    """语音转文字（STT）转写的协议。

    定义流式与最终化转写的接口。

    属性:
        audio_format (AudioFormat): 转写所期望的音频数据格式。
    """

    audio_format: AudioFormat

    def stream(
        self, windows: AsyncIterator[SpeechWindow]
    ) -> AsyncIterator[TranscriptSegment]:
        """将一句话转写为文本片段。

        若音频格式不受支持，应抛出异常。

        参数:
            windows: 待转写的音频窗口异步迭代器。

        返回:
            AsyncIterator[TranscriptSegment]: 带文本与时间信息的转写片段流。
        """
        ...


class TTS(Protocol):
    """文本转语音（TTS）合成的协议。

    定义将文本转换为音频的接口。

    属性:
        audio_format (AudioFormat): TTS 产出的音频数据格式。
    """

    audio_format: AudioFormat

    def stream(self, text: str) -> AsyncIterator[bytes]:
        """将文本合成为语音。

        参数:
            text: 要合成的文本。

        返回:
            AsyncIterator[bytes]: 指定格式下的原始 PCM 音频数据流。
        """
        ...


class MeetingProvider(Protocol):
    """会议提供方（Meeting Provider）的接口协议。

    提供方须实现音频输入/输出能力与会议控制功能。本协议保证各提供方接口一致。
    """

    @property
    def audio_reader(self) -> AudioReader:
        """获取提供方的音频读取端。

        返回:
            AudioReader: 音频输入源。
        """
        ...

    @property
    def audio_writer(self) -> AudioWriter:
        """获取提供方的音频写入端。

        返回:
            AudioWriter: 音频输出目标。
        """
        ...

    @property
    def video_reader(self) -> VideoReader:
        """获取提供方的视频读取端。

        返回:
            VideoReader: 视频输入源。
        """
        ...

    async def join(
        self,
        url: str | None = None,
        name: str | None = None,
        passcode: str | None = None,
    ) -> None:
        """加入会议。

        参数:
            url: 要加入的会议 URL。
            name: 在会议中显示的名称。
            passcode: 会议密码或通行码。
        """
        ...

    async def leave(self) -> None:
        """离开当前会议。"""
        ...

    async def send_chat_message(self, message: str) -> None:
        """向会议发送聊天消息。

        参数:
            message: 要发送的消息内容。
        """
        ...

    async def get_chat_history(self) -> MeetingChatHistory:
        """获取会议的聊天消息历史。

        返回:
            MeetingChatHistory: 会议的聊天历史。
        """
        ...

    async def get_participants(self) -> list[MeetingParticipant]:
        """获取会议参与者列表。

        返回:
            list[MeetingParticipant]: 会议中的参与者列表。
        """
        ...

    async def mute(self) -> None:
        """在会议中将自己静音。"""
        ...

    async def unmute(self) -> None:
        """在会议中取消静音。"""
        ...

    async def share_screen(self, url: str) -> None:
        """开始在会议中共享屏幕。

        参数:
            url: 共享时要展示的 URL。
        """
        ...

    async def stop_sharing(self) -> None:
        """停止在会议中共享屏幕。"""
        ...

    async def set_animation(self, animation: ActionAnimation | None) -> None:
        """在摄像头画面上设置动作动画。"""
        ...

    async def update_ui(self, update: UIUpdate) -> None:
        """更新会议提供方上的 UI。

        参数:
            update: 要应用的 UI 更新。
        """
        ...


class TranscriptionController(Protocol):
    """转写流程控制器的协议。

    定义启动与停止转写的接口。

    属性:
        reader (AudioReader): 用于转写的音频读取端。
        vad (VAD): 使用的语音活动检测服务。
        stt (STT): 用于转写的语音转文字服务。
    """

    reader: AudioReader
    vad: VAD
    stt: STT

    @property
    def no_speech_event(self) -> asyncio.Event:
        """获取表示未检测到语音的事件。

        返回:
            asyncio.Event: 未检测到语音时被设置的事件。
        """
        ...

    async def start(
        self, clock: Clock, transcript: Transcript, event_bus: EventBus
    ) -> None:
        """启动转写流程。

        参数:
            clock: 用于计时的时钟。
            transcript: 转写结果将写入的转写对象。
            event_bus: 用于发布事件的总线。
        """
        ...

    async def stop(self) -> None:
        """停止转写流程。"""
        ...


class SpeechController(Protocol):
    """语音输出控制器的协议。

    定义朗读文本的接口。

    属性:
        writer (AudioWriter): 用于输出的音频写入端。
        tts (TTS): 用于生成语音的文本转语音服务。
        no_speech_event (asyncio.Event): 未检测到语音时被设置的事件。
    """

    writer: AudioWriter
    tts: TTS
    no_speech_event: asyncio.Event

    async def start(
        self, clock: Clock, transcript: Transcript, event_bus: EventBus
    ) -> None:
        """启动语音输出流程。

        参数:
            clock: 用于计时的时钟。
            transcript: 语音相关记录将写入的转写对象。
            event_bus: 用于发布事件的总线。
        """
        ...

    async def stop(self) -> None:
        """停止语音输出流程。"""
        ...

    async def speak_text(self, text: str) -> None:
        """朗读给定文本。

        参数:
            text: 要朗读的文本。

        引发:
            SpeechInterruptedError: 若在完成前语音被中断。
        """
        ...
