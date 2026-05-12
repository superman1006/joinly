import asyncio
import contextlib
import logging
import re
from typing import Any, ClassVar

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from joinly.providers.browser.platforms.base import BaseBrowserPlatformController
from joinly.settings import get_settings
from joinly.types import MeetingChatHistory, MeetingChatMessage, MeetingParticipant

logger = logging.getLogger(__name__)


class TeamsBrowserPlatformController(BaseBrowserPlatformController):
    """管理 Teams 浏览器会议的控制器。"""

    url_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:https?://)?(?:[a-z0-9-]+\.)?(?:teams\.microsoft\.com|teams\.live\.com|teams\.microsoft\.us|dod\.teams\.microsoft\.us)/"
    )

    def __init__(self) -> None:
        """初始化 Teams 浏览器平台控制器。"""
        self._state: dict[str, Any] = {}

    @property
    def active_speaker(self) -> str | None:
        """获取 Teams 会议中当前发言人的名称。"""
        return self._state.get("active_speaker")

    async def join(
        self,
        page: Page,
        url: str,
        name: str,
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """加入 Teams 会议。

        参数:
            page: Playwright 的 Page 实例。
            url: The URL of the Teams meeting.
            name: 参与者显示名称。
            passcode: 会议密码（若需要）。
        """
        # 判断是否为政府版 Teams URL
        if "teams.microsoft.us" in url or "dod.teams.microsoft.us" in url:
            await self._join_gov_teams(page, url, name)
        else:
            await self._join_standard_teams(page, url, name)

        if not await self._check_joined(page):
            msg = "Join check failed: Failed to join the Teams meeting."
            raise RuntimeError(msg)

        await self._setup_active_speaker_observer(page)

    async def _join_standard_teams(
        self,
        page: Page,
        url: str,
        name: str,
    ) -> None:
        """加入标准 Teams 会议。

        参数:
            page: Playwright 的 Page 实例。
            url: The URL of the Teams meeting.
            name: 参与者显示名称。
        """
        await page.goto(url, wait_until="load", timeout=20000)

        async def _dismiss_dialog(page: Page) -> None:
            await page.click('div[role="dialog"] button', timeout=0)

        dismiss_dialog = asyncio.create_task(_dismiss_dialog(page))

        try:
            name_field = page.get_by_placeholder(re.compile("name", re.IGNORECASE))
            await name_field.fill(name, timeout=20000)

            join_btn = page.get_by_role(
                "button", name=re.compile(r"join", re.IGNORECASE)
            )
            await join_btn.click(timeout=10000)

        finally:
            if not dismiss_dialog.done():
                dismiss_dialog.cancel()

    async def _join_gov_teams(
        self,
        page: Page,
        url: str,
        name: str,
    ) -> None:
        """加入政府版 Teams 会议。

        Supports teams.microsoft.us or dod.teams.microsoft.us domains.

        参数:
            page: Playwright 的 Page 实例。
            url: The URL of the Teams meeting.
            name: 参与者显示名称。
        """
        # 政府版 Teams 可能有重定向，使用更长超时
        await page.goto(url, wait_until="load", timeout=60000)

        async def _dismiss_dialog(page: Page) -> None:
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.click('div[role="dialog"] button', timeout=1000)

        async def _click_join_browser(page: Page) -> None:
            with contextlib.suppress(PlaywrightTimeoutError):
                btn_pattern = re.compile(r"join.*browser|continue.*web", re.IGNORECASE)
                join_browser_btn = page.get_by_role("button", name=btn_pattern)
                await join_browser_btn.click(timeout=1000)

        dismiss_dialog = asyncio.create_task(_dismiss_dialog(page))
        join_browser = asyncio.create_task(_click_join_browser(page))

        try:
            name_field = page.locator(
                'input[placeholder*="name" i], input[aria-label*="name" i]'
            ).first
            await name_field.fill(name, timeout=40000)

            join_btn = page.get_by_role(
                "button", name=re.compile(r"join", re.IGNORECASE)
            )
            await join_btn.click(timeout=10000)

        finally:
            for task in [dismiss_dialog, join_browser]:
                if not task.done():
                    task.cancel()

    async def leave(self, page: Page) -> None:
        """离开 Teams 会议。

        参数:
            page: Playwright 的 Page 实例。
        """
        leave_btn = page.get_by_role("button", name=re.compile(r"leave", re.IGNORECASE))
        if not await leave_btn.is_visible():
            msg = "Leave button not found or not visible."
            raise RuntimeError(msg)
        await leave_btn.click(timeout=1000)
        await page.wait_for_timeout(500)

    async def send_chat_message(self, page: Page, message: str) -> None:
        """在 Teams 会议中发送聊天消息。

        参数:
            page: Playwright 的 Page 实例。
            message: 要发送的消息内容。
        """
        await self._open_chat(page)

        chat_input = page.locator("div[contenteditable='true']")
        if not await chat_input.is_visible():
            msg = "Chat input not found or not visible."
            raise RuntimeError(msg)
        await chat_input.fill(message)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:
        """获取 Teams 会议的聊天历史。

        参数:
            page: Playwright 的 Page 实例。

        返回:
            MeetingChatHistory: 会议的聊天历史。
        """
        await self._open_chat(page)

        messages: list[MeetingChatMessage] = []

        chat_items = await page.locator('[data-tid="chat-pane-item"]').all()
        for el in chat_items:
            content_el = el.locator('[data-tid="chat-pane-message"]')
            if not await content_el.count():
                continue
            text = (await content_el.first.inner_text()).strip()
            ts = await el.locator("time[datetime]").first.get_attribute("datetime")
            author_locator = el.locator('[data-tid="message-author-name"]').first
            sender_text = await author_locator.text_content() or ""
            sender = sender_text.strip() or None
            messages.append(MeetingChatMessage(text=text, timestamp=ts, sender=sender))

        return MeetingChatHistory(messages=messages)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """获取 Teams 会议的参与者列表。

        参数:
            page: Playwright 的 Page 实例。

        返回:
            list[MeetingParticipant]: 会议中的参与者列表。
        """
        participants_list = page.locator('div[aria-label="Attendees"][role="tree"]')
        is_participant_list_visible = await participants_list.is_visible()

        if not is_participant_list_visible:
            participants_button = page.get_by_role(
                "button", name=re.compile(r"^people", re.IGNORECASE)
            )
            if not await participants_button.is_visible():
                msg = "Participants button not found or not visible."
                raise RuntimeError(msg)
            await participants_button.click()
            await page.wait_for_timeout(1000)
            if not await participants_list.is_visible():
                await page.wait_for_timeout(1000)

        participants: list[MeetingParticipant] = []
        for item in await participants_list.locator(
            "[data-cid='roster-participant'][aria-label]"
        ).all():
            if aria_label := await item.get_attribute("aria-label"):
                labels = aria_label.split(", ")
                name = labels[0].strip()
                infos = labels[1:] if len(labels) > 1 else []
                participants.append(MeetingParticipant(name=name, infos=infos))

        return participants

    async def mute(self, page: Page) -> None:
        """在 Teams 会议中将参与者静音。

        参数:
            page: Playwright 的 Page 实例。
        """
        mute_btn = page.get_by_role("button", name=re.compile(r"^mute", re.IGNORECASE))
        if await mute_btn.is_visible():
            await mute_btn.click(timeout=1000)
        elif not await page.get_by_role(
            "button", name=re.compile(r"^unmute", re.IGNORECASE)
        ).is_visible():
            msg = "Mute button not found or not visible."
            raise RuntimeError(msg)

    async def unmute(self, page: Page) -> None:
        """在 Teams 会议中取消参与者静音。

        参数:
            page: Playwright 的 Page 实例。
        """
        unmute_btn = page.get_by_role(
            "button", name=re.compile(r"^unmute", re.IGNORECASE)
        )
        if await unmute_btn.is_visible():
            await unmute_btn.click(timeout=1000)
        elif not await page.get_by_role(
            "button", name=re.compile(r"^mute", re.IGNORECASE)
        ).is_visible():
            msg = "Unmute button not found or not visible."
            raise RuntimeError(msg)

    async def share_screen(self, page: Page) -> None:
        """开始在 Teams 会议中共享屏幕。

        Clicks the share toolbar button.  If Teams opens a share tray
        with options (Screen, Window, …), selects the "Screen" option
        to trigger ``getDisplayMedia``.

        参数:
            page: Playwright 的 Page 实例。
        """
        share_btn = page.get_by_role(
            "button", name=re.compile(r"share\b", re.IGNORECASE)
        )
        if not await share_btn.is_visible():
            msg = "Share button not found or not visible."
            raise RuntimeError(msg)
        await share_btn.click(timeout=2000)
        await page.wait_for_timeout(1000)

        # Teams 可能显示共享托盘 — 若存在则选择「屏幕」
        screen_option = page.locator(
            'button:has-text("Screen"), '
            'button:has-text("Entire screen"), '
            '[role="menuitem"]:has-text("Screen"), '
            '[aria-label*="screen" i][role="button"], '
            '[aria-label*="Screen"][role="menuitem"]'
        ).first
        try:
            await screen_option.wait_for(state="visible", timeout=3000)
            await screen_option.click(timeout=2000)
            await page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            # 无托盘 — 共享按钮直接触发 getDisplayMedia
            pass

    async def stop_sharing(self, page: Page) -> None:
        """停止在 Teams 会议中共享屏幕。

        The Share button is a toggle — clicking it again stops sharing.

        参数:
            page: Playwright 的 Page 实例。
        """
        share_btn = page.get_by_role(
            "button",
            name=re.compile(r"(share|stop\s+(sharing|presenting))\b", re.IGNORECASE),
        )
        if not await share_btn.first.is_visible():
            msg = "Share button not found or not visible."
            raise RuntimeError(msg)
        await share_btn.first.click(timeout=2000)
        await page.wait_for_timeout(500)

    async def _check_joined(self, page: Page, timeout: float = 20) -> bool:  # noqa: ASYNC109
        """检查 Teams 会议是否已成功加入。

        Looks for lobby indicators (various "waiting" messages across
        Teams v1 and v2) or the *Leave* button which confirms the
        participant is inside the meeting.

        参数:
            page: Playwright 的 Page 实例。
            timeout: 检查是否加入成功的超时时间（秒）。

        返回:
            bool: True if joined, False otherwise.
        """
        locators = [
            page.locator("span >> text=/please wait/i"),
            page.locator("span >> text=/will let you in/i"),
            page.locator("span >> text=/waiting/i"),
            page.locator("span >> text=/someone in the meeting/i"),
            page.get_by_role("button", name=re.compile(r"leave", re.IGNORECASE)),
        ]

        tasks = [
            asyncio.create_task(loc.wait_for(state="visible", timeout=0))
            for loc in locators
        ]

        try:
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED, timeout=timeout
            )
            return any(not task.exception() for task in done)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def _open_chat(self, page: Page) -> None:
        """在 Teams 会议中打开聊天。"""
        chat_input = page.locator("div[contenteditable='true']")
        is_chat_visible = await chat_input.is_visible()

        if not is_chat_visible:
            chat_button = page.get_by_role(
                "button", name=re.compile(r"^chat", re.IGNORECASE)
            )
            if not await chat_button.is_visible():
                msg = "Chat button not found or not visible."
                raise RuntimeError(msg)
            await chat_button.click()
            await page.wait_for_timeout(1000)
            if not await chat_input.is_visible():
                await page.wait_for_timeout(2000)

    async def _setup_active_speaker_observer(self, page: Page) -> None:
        """为 Teams 设置当前发言人观察逻辑。"""
        await page.expose_binding(
            "report",
            lambda _, name: self._state.update({"active_speaker": name}),
        )
        await page.evaluate(
            """
            (nameArg) => {
                const emit = n => window.report(n);
                const find = () => {
                    for (
                        const t of document.querySelectorAll(
                            'div[data-tid="stage-layout"] div[role="menuitem"]'
                        )
                    ) {
                        if (!!t.querySelector(
                            'div[data-tid="voice-level-stream-outline"].vdi-frame-occlusion'
                        )) {
                            let el = t.querySelector(
                                'div[data-tid="participant-info-nametag"]'
                            );
                            if (!el) {
                                el = t.querySelector('div:not(:has(*)):not(:empty)');
                            }
                            const name = el?.textContent.trim();
                            if (name && name.length > 0 && name !== nameArg)
                                return name;
                        }
                    }
                    return null;
                };

                let last = null, cur;
                new MutationObserver(() => {
                    cur = find();
                    if (cur !== last) { last = cur; emit(cur); }
                }).observe(
                    document,
                    {
                        subtree: true,
                        childList: true,
                        attributes: true,
                        attributeFilter: ['class']
                    }
                );
                emit(find());
            }
            """,
            get_settings().name,
        )
