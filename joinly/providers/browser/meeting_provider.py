import asyncio
import io
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Self

from PIL import Image, ImageOps
from playwright.async_api import Page

from joinly.core import AudioReader, AudioWriter, VideoReader
from joinly.providers.base import BaseMeetingProvider
from joinly.providers.browser.browser_session import BrowserSession
from joinly.providers.browser.camera_feed import CameraFeed
from joinly.providers.browser.devices.pulse_server import PulseServer
from joinly.providers.browser.devices.virtual_display import VirtualDisplay
from joinly.providers.browser.devices.virtual_microphone import VirtualMicrophone
from joinly.providers.browser.devices.virtual_speaker import VirtualSpeaker
from joinly.providers.browser.platforms import (
    BrowserPlatformController,
    FeishuBrowserPlatformController,
    GoogleMeetBrowserPlatformController,
    TeamsBrowserPlatformController,
    ZoomBrowserPlatformController,
)
from joinly.providers.browser.screen_share import remove_overlay, setup_content_stream
from joinly.settings import get_settings
from joinly.types import (
    ActionAnimation,
    AudioChunk,
    MeetingChatHistory,
    MeetingParticipant,
    ProviderNotSupportedError,
    UIAnimationContent,
    UIHtmlContent,
    UIUpdate,
    VideoSnapshot,
)

logger = logging.getLogger(__name__)

PLATFORMS: list[type[BrowserPlatformController]] = [
    GoogleMeetBrowserPlatformController,
    TeamsBrowserPlatformController,
    ZoomBrowserPlatformController,
    FeishuBrowserPlatformController,
]


class _SpeakerInjectedAudioReader(AudioReader):
    """向虚拟扬声器注入音频的音频读取端。"""

    def __init__(
        self, reader: AudioReader, get_reader: Callable[[], str | None]
    ) -> None:
        """使用虚拟扬声器初始化音频读取端。"""
        self._reader = reader
        self._get_reader = get_reader
        self.audio_format = reader.audio_format

    async def read(self) -> AudioChunk:
        """读取音频数据并注入虚拟扬声器。"""
        chunk = await self._reader.read()
        return AudioChunk(
            data=chunk.data,
            time_ns=chunk.time_ns,
            speaker=self._get_reader(),
        )


class BrowserMeetingProvider(BaseMeetingProvider, VideoReader):
    """通过 Web 浏览器加入会议的会议提供方。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        reader_byte_depth: int | None = None,
        writer_byte_depth: int | None = None,
        display_size: tuple[int, int] = (1280, 720),
        snapshot_size: tuple[int, int] = (512, 288),
        vnc_server: bool = False,
        vnc_server_port: int = 5900,
    ) -> None:
        """初始化浏览器会议提供方。

        参数:
            reader_byte_depth (int | None): 虚拟扬声器的字节深度（默认 None）。
            writer_byte_depth (int | None): 虚拟麦克风的字节深度（默认 None）。
            display_size (tuple[int, int]): 虚拟显示器与屏幕共享分辨率
                （默认 (1280, 720)）。
            snapshot_size (tuple[int, int]): 视频快照尺寸（默认 (512, 288)）。
            vnc_server (bool): 是否为虚拟显示器启动 VNC 服务。
            vnc_server_port (int): VNC 服务端口。
        """
        self.snapshot_size = snapshot_size
        self._display_size = display_size
        self._env = os.environ.copy()
        self._pulse_server = PulseServer(env=self._env)
        self._virtual_display = VirtualDisplay(
            env=self._env,
            size=display_size,
            use_vnc_server=vnc_server,
            vnc_port=vnc_server_port,
        )
        self._virtual_speaker = (
            VirtualSpeaker(env=self._env)
            if not reader_byte_depth
            else VirtualSpeaker(env=self._env, byte_depth=reader_byte_depth)
        )
        self._virtual_microphone = (
            VirtualMicrophone(env=self._env)
            if not writer_byte_depth
            else VirtualMicrophone(env=self._env, byte_depth=writer_byte_depth)
        )
        self._browser_session = BrowserSession(env=self._env)
        self._services = [
            self._pulse_server,
            self._virtual_display,
            self._virtual_speaker,
            self._virtual_microphone,
            self._browser_session,
        ]

        self._page: Page | None = None
        self._content_page: Page | None = None
        self._is_sharing: bool = False
        self._platform_controller: BrowserPlatformController | None = None
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()

        self._camera_feed = CameraFeed(self._virtual_microphone)
        self._speaker_injected_virtual_speaker = _SpeakerInjectedAudioReader(
            self._virtual_speaker,
            lambda: (
                self._platform_controller.active_speaker
                if self._platform_controller
                else None
            ),
        )

    @property
    def audio_reader(self) -> AudioReader:
        """获取音频读取端。"""
        return self._speaker_injected_virtual_speaker

    @property
    def audio_writer(self) -> AudioWriter:
        """获取音频写入端。"""
        return self._camera_feed.audio_writer

    @property
    def video_reader(self) -> VideoReader:
        """获取视频读取端。"""
        return self

    async def __aenter__(self) -> Self:
        """进入上下文管理器。"""
        try:
            for service in self._services:
                await self._stack.enter_async_context(service)

        except Exception:
            await self._stack.aclose()
            raise

        return self

    async def __aexit__(self, *_exc: object) -> None:
        """退出上下文。"""
        try:
            if self._page is not None and not self._page.is_closed():
                await self.leave()
        finally:
            await self._stack.aclose()

    @asynccontextmanager
    async def _action_guard(
        self, action: str
    ) -> AsyncIterator[tuple[Page, BrowserPlatformController]]:
        """在加锁与统一错误处理下保护操作的上下文管理器。

        参数:
            action: 被保护的操作名称，用于日志（如 "join"、"leave" 等）。

        产生:
            tuple[Page, BrowserPlatformController]: 当前 Page 与平台专用控制器。
        """
        if (
            self._page is None
            or self._page.is_closed()
            or self._platform_controller is None
        ):
            msg = f"Failed to perform '{action}'. Currently not in a meeting."
            logger.error(msg)
            raise RuntimeError(msg)

        async with self._lock:
            try:
                yield self._page, self._platform_controller
            except Exception as e:
                msg = f"Failed to perform '{action}'."
                logger.exception(msg)
                if isinstance(e, (ProviderNotSupportedError, ValueError)):
                    raise
                raise RuntimeError(msg) from None
            else:
                logger.info("Successfully performed '%s'.", action)

    async def _get_platform_controller(self, url: str) -> BrowserPlatformController:
        """根据 URL 获取对应平台的会议控制器。

        参数:
            url: 会议的 URL。

        返回:
            BrowserPlatformController: 平台专用的会议控制器。

        引发:
            RuntimeError: 若 URL 没有匹配的平台控制器。
        """
        for platform_controller_type in PLATFORMS:
            if platform_controller_type.url_pattern.match(url):
                return platform_controller_type()

        msg = (
            f"No supported platform found for URL: {url}. "
            "Supported platforms: "
            f"{
                ', '.join(
                    pc.__name__.removesuffix('BrowserPlatformController')
                    for pc in PLATFORMS
                )
            }."
        )
        raise RuntimeError(msg)

    async def _cleanup_content_page(self) -> None:
        """若存在内容页则关闭并重置共享状态。"""
        if self._content_page and not self._content_page.is_closed():
            await self._content_page.close()
        self._content_page = None
        self._is_sharing = False

    async def join(
        self,
        url: str | None = None,
        name: str | None = None,
        passcode: str | None = None,
    ) -> None:
        """加入会议。

        参数:
            url: 要加入的会议 URL。
            name: 参与者显示名称；若为 None，则使用配置中的默认名称。
            passcode: 会议密码或通行码（若需要）。
        """
        if not url:
            msg = "Meeting URL is required to join a meeting."
            logger.error(msg)
            raise ValueError(msg)

        if self._page is not None and not self._page.is_closed():
            msg = "Meeting already joined. Leave the meeting before joining a new one."
            logger.error(msg)
            raise RuntimeError(msg)

        self._page = await self._browser_session.get_page()
        await self._camera_feed.install(self._page)
        try:
            self._platform_controller = await self._get_platform_controller(url)
        except RuntimeError:
            await self._page.close()
            self._page = None
            raise

        if name is None:
            name = get_settings().name

        async with self._action_guard("join") as (page, controller):
            try:
                await controller.join(page, url, name=name, passcode=passcode)
            except Exception:
                await self._page.close()
                self._page = None
                self._platform_controller = None
                raise

    async def leave(self) -> None:
        """离开当前会议。"""
        async with self._action_guard("leave") as (page, controller):
            try:
                if self._is_sharing:
                    await self._cleanup_content_page()
                    await page.bring_to_front()
                await controller.leave(page)
            except RuntimeError:
                logger.warning(
                    "Failed to leave the meeting, forcing page close.", exc_info=True
                )
            finally:
                self._platform_controller = None
                await self._camera_feed.stop()
                await self._cleanup_content_page()
                if self._page is not None and not self._page.is_closed():
                    await self._page.close()
                self._page = None

    async def send_chat_message(self, message: str) -> None:
        """在会议中发送聊天消息。

        参数:
            message: 要发送的消息内容。
        """
        async with self._action_guard("send_chat_message") as (page, controller):
            await controller.send_chat_message(page, message)

    async def get_chat_history(self) -> MeetingChatHistory:
        """获取会议的聊天历史。

        返回:
            MeetingChatHistory: 会议的聊天历史。
        """
        async with self._action_guard("get_chat_history") as (page, controller):
            return await controller.get_chat_history(page)

    async def get_participants(self) -> list[MeetingParticipant]:
        """获取会议参与者列表。

        返回:
            list[MeetingParticipant]: 会议中的参与者列表。
        """
        async with self._action_guard("get_participants") as (page, controller):
            return await controller.get_participants(page)

    async def mute(self) -> None:
        """在会议中将自己静音。"""
        async with self._action_guard("mute") as (page, controller):
            await controller.mute(page)

    async def unmute(self) -> None:
        """在会议中取消静音。"""
        async with self._action_guard("unmute") as (page, controller):
            await controller.unmute(page)

    async def share_screen(self, url: str) -> None:
        """开始在会议中共享屏幕。

        Opens *url* in a separate browser tab and streams its content
        via a full-screen canvas overlay on the meeting tab.  Tab
        self-capture ensures the platform receives a real
        ``getDisplayMedia`` stream while participants see only the
        shared content.

        参数:
            url: URL to display while sharing.
        """
        if self._is_sharing:
            msg = (
                "Already sharing screen. "
                "Stop the current share before starting a new one."
            )
            raise RuntimeError(msg)

        content_page = await self._browser_session.get_page()
        await content_page.goto(url, wait_until="load", timeout=20000)

        try:
            async with self._action_guard("share_screen") as (page, controller):
                await setup_content_stream(page, content_page, self._display_size)
                await controller.share_screen(page)
                # 短暂等待 getDisplayMedia 完成
                ok = None
                for _ in range(10):
                    ok = await page.evaluate("() => window.__scShareOk")
                    if ok is not None:
                        break
                    await page.wait_for_timeout(500)
                if not ok:
                    logger.error("getDisplayMedia was denied or errored.")
                    msg = "Screen sharing failed to start."
                    raise ProviderNotSupportedError(msg)
                self._content_page = content_page
                content_page = None  # 所有权已转移
                self._is_sharing = True
        finally:
            if content_page and not content_page.is_closed():
                await content_page.close()

    async def stop_sharing(self) -> None:
        """停止在会议中共享屏幕。"""
        if not self._is_sharing:
            return
        async with self._action_guard("stop_sharing") as (page, controller):
            try:
                await page.bring_to_front()
                await controller.stop_sharing(page)
                await remove_overlay(page)
            finally:
                await self._cleanup_content_page()

    async def set_animation(self, animation: ActionAnimation | None) -> None:
        """在摄像头画面上设置动作动画。"""
        self._camera_feed.set_effect(animation)

    async def update_ui(self, update: UIUpdate) -> None:
        """更新摄像头画面上的 UI。"""
        if isinstance(update.content, UIAnimationContent):
            self._camera_feed.set_effect(update.content.animation)
        elif isinstance(update.content, UIHtmlContent):
            logger.warning("HTML UI content not yet supported")

    async def snapshot(self) -> VideoSnapshot:
        """捕获当前视频帧的快照。

        返回:
            VideoSnapshot: 当前视频帧的快照。
        """
        if not self._page or self._page.is_closed():
            msg = "Cannot take snapshot. Not currently in a meeting."
            logger.error(msg)
            raise RuntimeError(msg)

        raw = await self._page.screenshot(type="png")
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = ImageOps.crop(img, border=int(min(*img.size) * 0.1))
        img = ImageOps.fit(
            img,
            self.snapshot_size,
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )

        buf = io.BytesIO()
        img.save(buf, format="jpeg", quality=90, optimize=True, progressive=True)

        return VideoSnapshot(data=buf.getvalue(), media_type="image/jpeg")
