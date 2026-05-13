import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Self

from playwright.async_api import Browser as PlaywrightBrowser
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from joinly.utils.logging import LOGGING_TRACE

logger = logging.getLogger(__name__)

_CDP_RE = re.compile(r"DevTools listening on (ws://.*)")


class BrowserSession:
    """使用 Playwright 表示浏览器会话的类。"""

    def __init__(self, *, env: dict[str, str] | None = None, cdp_port: int = 0) -> None:
        """初始化浏览器参数。

        参数:
            env: 浏览器进程使用的环境变量（默认 None，表示使用当前环境副本）。
            cdp_port (int): Chrome DevTools 协议监听端口（默认 0 表示自动分配）。
        """
        self._env: dict[str, str] = env if env is not None else os.environ.copy()
        self._cdp_port: int = cdp_port

        self._proc: asyncio.subprocess.Process | None = None
        self._profile_dir: tempfile.TemporaryDirectory | None = None
        self._playwright: Playwright | None = None
        self._pw_browser: PlaywrightBrowser | None = None
        self._pw_context: BrowserContext | None = None
        self._default_page: Page | None = None
        self._pages = list[Page]()
        self.cdp_url: str | None = None

    async def __aenter__(self) -> Self:
        """启动并连接 Playwright 浏览器。"""
        self._playwright = await async_playwright().start()

        # 优先使用系统安装的 Google Chrome（可通过飞书浏览器检测）
        _chrome_candidates = [
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/google-chrome"),
            Path("/opt/google/chrome/chrome"),
        ]
        bin_path = next(
            (p for p in _chrome_candidates if p.exists()),
            Path(self._playwright.chromium.executable_path),
        )
        logger.debug("Browser binary path: %s", bin_path)
        if not bin_path.exists():
            msg = "Browser binary not found"
            logger.error(msg)
            raise RuntimeError(msg)

        self._profile_dir = tempfile.TemporaryDirectory(prefix="pw-profile_")
        logger.debug("Profile directory created at: %s", self._profile_dir.name)

        logger.debug("Launching Chromium browser.")
        self._proc = await asyncio.create_subprocess_exec(
            str(bin_path),
            f"--remote-debugging-port={self._cdp_port}",
            f"--user-data-dir={self._profile_dir.name}",
            "--use-fake-ui-for-media-stream",
            "--alsa-output-device=pulse",
            f"--alsa-input-device={self._env.get('PULSE_SOURCE')}",
            "--autoplay-policy=no-user-gesture-required",
            "--allow-http-screen-capture",
            "--auto-select-desktop-capture-source=Entire",
            "--enable-usermedia-screen-capturing",
            "--enable-features=WebRTCPipeWireCapturer",
            "--ozone-platform=x11",
            "--use-gl=swiftshader",  # 软件渲染 WebGL，无需真实 GPU
            "--disable-gpu-sandbox",
            "--disable-focus-on-load",
            "--window-size=1280,720",
            "--lang=zh-CN",
            "--test-type",
            "--no-sandbox",  # Docker 内必需
            "--disable-dev-shm-usage",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36",
            "--no-xshm",
            "--force-device-scale-factor=1",
            "--disable-features=TranslateUI,MediaRouter,WebRtcAutomaticGainControl",
            "--disable-backgrounding-occluded-windows",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            start_new_session=True,
        )
        logger.debug("Chromium browser launched.")

        while line := await self._proc.stderr.readline():  # type: ignore[attr-defined]
            logger.log(LOGGING_TRACE, "[chromium] %s", line.decode().strip())
            m = _CDP_RE.search(line.decode())
            if m:
                cdp_endpoint = m.group(1)
                break
        else:
            self._proc.terminate()
            msg = "Could not find DevTools URL in stderr"
            logger.error(msg)
            raise RuntimeError(msg)
        logger.debug("DevTools URL: %s", cdp_endpoint)
        self.cdp_url = cdp_endpoint

        self._pw_browser = await self._playwright.chromium.connect_over_cdp(
            cdp_endpoint
        )
        self._pw_context = self._pw_browser.contexts[0]
        # Chrome UA — 与系统安装的 Google Chrome 保持一致
        _ua = (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        )
        await self._pw_context.set_extra_http_headers(
            {
                "User-Agent": _ua,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Sec-CH-UA": (
                    '"Google Chrome";v="136", "Chromium";v="136", "Not-A.Brand";v="99"'
                ),
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Linux"',
            }
        )
        await self._pw_context.add_init_script(f"""
            Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
            Object.defineProperty(navigator, 'vendor', {{get: () => 'Google Inc.'}});
            Object.defineProperty(navigator, 'userAgent', {{get: () => '{_ua}'}});
            window.chrome = {{runtime: {{}}, loadTimes: ()=>{{}}, csi: ()=>{{}}}};
            const _brands = [
                {{brand: 'Google Chrome', version: '136'}},
                {{brand: 'Chromium', version: '136'}},
                {{brand: 'Not-A.Brand', version: '99'}},
            ];
            Object.defineProperty(navigator, 'userAgentData', {{
                get: () => ({{
                    brands: _brands,
                    mobile: false,
                    platform: 'Linux',
                    getHighEntropyValues: async () => ({{
                        brands: _brands,
                        mobile: false,
                        platform: 'Linux',
                        architecture: 'x86',
                        bitness: '64',
                    }}),
                }}),
            }});
        """)
        self._default_page = (
            self._pw_context.pages[0] if self._pw_context.pages else None
        )

        logger.debug("Playwright started.")

        return self

    async def __aexit__(self, *exc: object) -> None:
        """停止浏览器。"""
        logger.debug("Stopping browser.")

        for page in self._pages:
            if page is not self._default_page and not page.is_closed():
                await page.close()
        if self._playwright:
            await self._playwright.stop()

        if self._proc and self._proc.returncode is None:
            logger.debug("Terminating browser process.")
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1)
            except TimeoutError:
                logger.warning("Browser process did not terminate, killing it.")
                self._proc.kill()
                await self._proc.wait()
        logger.debug("Browser stopped.")

        if self._profile_dir is not None:
            self._profile_dir.cleanup()
            logger.debug("Profile directory removed: %s", self._profile_dir.name)

        self._pw_context = None
        self._pw_browser = None
        self._playwright = None
        self._proc = None
        self._profile_dir = None
        self._default_page = None
        self._pages = []
        self.cdp_url = None

    async def get_page(self) -> Page:
        """在浏览器上下文中打开新页面。"""
        if self._pw_context is None:
            msg = "Playwright context is not initialized."
            raise RuntimeError(msg)

        page = await self._pw_context.new_page()
        logger.debug("New page created in the browser context.")

        page.on(
            "console",
            lambda msg: logger.log(
                LOGGING_TRACE, "[console][%s] %s", msg.type, msg.text
            ),
        )
        self._pages.append(page)

        return page
