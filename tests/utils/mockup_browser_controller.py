import re
from typing import ClassVar

from playwright.async_api import Page

from joinly.providers.browser.platforms.base import BaseBrowserPlatformController


class MockupBrowserPlatformController(BaseBrowserPlatformController):
    """管理模拟会议页面的控制器。"""

    url_pattern: ClassVar[re.Pattern[str]] = re.compile(r".*")

    async def join(
        self,
        page: Page,
        url: str,
        name: str,
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """加入模拟会议。

        参数:
            page: Playwright 的 Page 实例。
            url: 模拟会议的 URL。
            name: 参与者显示名称。
            passcode: 会议密码（若需要）。
        """
        await page.goto(url, wait_until="load", timeout=2000)
        await page.fill("#name", name, timeout=1000)
        await page.click("#join", timeout=1000)

    async def leave(self, page: Page) -> None:
        """离开模拟会议。

        参数:
            page: Playwright 的 Page 实例。
        """
        await page.click("#leave", timeout=1000)
