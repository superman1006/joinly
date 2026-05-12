import asyncio
import contextlib
import logging
import re
from typing import Any, ClassVar

from playwright.async_api import Page

from joinly.providers.browser.platforms.base import BaseBrowserPlatformController
from joinly.types import MeetingChatHistory, MeetingChatMessage, MeetingParticipant

logger = logging.getLogger(__name__)

# 飞书视频会议 Web 端的消息最大字符数限制
_MAX_MESSAGE_LENGTH = 1000


class FeishuBrowserPlatformController(BaseBrowserPlatformController):
    """管理飞书（Lark）浏览器视频会议的控制器。

    支持 vc.feishu.cn 与 vc.larksuite.com 两种域名，
    URL 格式示例：https://vc.feishu.cn/j/905158212
    """

    url_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:https?://)?vc\.(?:feishu\.cn|larksuite\.com)/j/"
    )

    def __init__(self) -> None:
        """初始化飞书浏览器平台控制器。"""
        self._state: dict[str, Any] = {}

    @property
    def active_speaker(self) -> str | None:
        """获取飞书会议中当前发言人的名称。"""
        return self._state.get("active_speaker")

    async def join(
        self,
        page: Page,
        url: str,
        name: str,
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """加入飞书视频会议。

        参数:
            page: Playwright 的 Page 实例。
            url: 飞书会议 URL（格式：https://vc.feishu.cn/j/<会议号>）。
            name: 参与者显示名称。
            passcode: 会议密码（若需要）。
        """
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 等待入会前大厅页面加载完成
        await page.wait_for_timeout(2000)

        # 填写参与者姓名（飞书大厅通常有姓名输入框）
        name_input = page.locator(
            "input[placeholder*='名字'], input[placeholder*='name'], "
            "input[placeholder*='Name'], input[data-testid*='name']"
        ).first
        with contextlib.suppress(Exception):
            if await name_input.is_visible(timeout=5000):
                await name_input.click(click_count=3)
                await name_input.fill(name)
                await page.wait_for_timeout(300)

        # 点击「加入会议」/「Join」按钮
        join_btn = page.locator(
            "button:has-text('加入会议'), button:has-text('Join'), "
            "button:has-text('立即加入'), button:has-text('Enter')"
        ).first
        await join_btn.click(timeout=10000)

        # 等待成功进入会议（底部工具栏出现即视为成功）
        if not await self._check_joined(page):
            msg = "加入飞书会议失败：无法确认已进入会议室。"
            raise RuntimeError(msg)

        await self._setup_active_speaker_observer(page)

    async def leave(self, page: Page) -> None:
        """离开飞书视频会议。

        参数:
            page: Playwright 的 Page 实例。
        """
        # 点击「结束/离开」按钮
        leave_btn = page.locator(
            "button[aria-label*='离开'], button[aria-label*='Leave'], "
            "button[aria-label*='结束'], button[aria-label*='End'], "
            "div[data-testid*='leave'], div[data-testid*='end']"
        ).first
        with contextlib.suppress(Exception):
            if await leave_btn.is_visible(timeout=3000):
                await leave_btn.click(timeout=3000)
                await page.wait_for_timeout(500)
                # 确认离开弹窗
                confirm = page.locator(
                    "button:has-text('离开'), button:has-text('Leave'), "
                    "button:has-text('确认'), button:has-text('Confirm')"
                ).first
                if await confirm.is_visible(timeout=2000):
                    await confirm.click(timeout=2000)
                return

        # 兜底：直接导航离开
        await page.goto("about:blank")

    async def send_chat_message(self, page: Page, message: str) -> None:
        """在飞书会议中发送聊天消息。

        参数:
            page: Playwright 的 Page 实例。
            message: 要发送的消息内容。
        """
        if len(message) > _MAX_MESSAGE_LENGTH:
            msg = (
                f"消息超出最大长度 {_MAX_MESSAGE_LENGTH} 字符，"
                f"当前长度：{len(message)}。"
            )
            raise ValueError(msg)

        await self._open_chat(page)

        chat_input = page.locator(
            "div[contenteditable='true'][data-testid*='chat'], "
            "textarea[placeholder*='发送消息'], textarea[placeholder*='Send'], "
            "div[contenteditable='true'][placeholder*='发送消息'], "
            "div[contenteditable='true'][placeholder*='Send']"
        ).first
        if not await chat_input.is_visible(timeout=5000):
            msg = "未找到聊天输入框或输入框不可见。"
            raise RuntimeError(msg)

        await chat_input.click()
        await chat_input.fill(message)
        await page.wait_for_timeout(300)
        await page.keyboard.press("Enter")

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:
        """获取飞书会议的聊天消息历史。

        参数:
            page: Playwright 的 Page 实例。

        返回:
            MeetingChatHistory: 会议的聊天历史。
        """
        await self._open_chat(page)
        await page.wait_for_timeout(500)

        messages: list[MeetingChatMessage] = []

        # 飞书聊天消息容器
        msg_items = await page.locator(
            "[data-testid*='chat-message'], [class*='chat-message'], "
            "[class*='message-item']"
        ).all()

        for item in msg_items:
            with contextlib.suppress(Exception):
                sender_el = item.locator(
                    "[class*='sender'], [class*='username'], [class*='name']"
                ).first
                text_el = item.locator(
                    "[class*='content'], [class*='text'], [class*='body']"
                ).first
                sender_cnt = await sender_el.count()
                text_cnt = await text_el.count()
                sender = (await sender_el.inner_text()).strip() if sender_cnt else None
                text = (await text_el.inner_text()).strip() if text_cnt else None
                if text:
                    messages.append(MeetingChatMessage(text=text, sender=sender))

        return MeetingChatHistory(messages=messages)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """获取飞书会议的参与者列表。

        参数:
            page: Playwright 的 Page 实例。

        返回:
            list[MeetingParticipant]: 会议中的参与者列表。
        """
        # 打开参与者面板
        members_btn = page.locator(
            "button[aria-label*='参与者'], button[aria-label*='Members'], "
            "button[aria-label*='Participants'], "
            "div[data-testid*='members'], div[data-testid*='participants']"
        ).first
        with contextlib.suppress(Exception):
            if await members_btn.is_visible(timeout=3000):
                await members_btn.click(timeout=3000)
                await page.wait_for_timeout(1000)

        participants: list[MeetingParticipant] = []

        member_items = await page.locator(
            "[data-testid*='member-item'], [class*='member-item'], "
            "[class*='participant-item']"
        ).all()

        for item in member_items:
            with contextlib.suppress(Exception):
                name_el = item.locator(
                    "[class*='name'], [class*='username']"
                ).first
                name_cnt = await name_el.count()
                name = (await name_el.inner_text()).strip() if name_cnt else None
                if name:
                    participants.append(MeetingParticipant(name=name, infos=[]))

        return participants

    async def mute(self, page: Page) -> None:
        """在飞书会议中将自己静音。

        参数:
            page: Playwright 的 Page 实例。
        """
        mute_btn = page.locator(
            "button[aria-label*='关闭麦克风'], button[aria-label*='Mute'], "
            "button[aria-label*='静音'], div[data-testid*='mute-mic']"
        ).first
        if await mute_btn.is_visible(timeout=3000):
            await mute_btn.click(timeout=3000)
        else:
            logger.warning("未找到飞书静音按钮")

    async def unmute(self, page: Page) -> None:
        """在飞书会议中取消静音。

        参数:
            page: Playwright 的 Page 实例。
        """
        unmute_btn = page.locator(
            "button[aria-label*='开启麦克风'], button[aria-label*='Unmute'], "
            "button[aria-label*='取消静音'], div[data-testid*='unmute-mic']"
        ).first
        if await unmute_btn.is_visible(timeout=3000):
            await unmute_btn.click(timeout=3000)
        else:
            logger.warning("未找到飞书取消静音按钮")

    async def share_screen(self, page: Page) -> None:
        """开始在飞书会议中共享屏幕。

        参数:
            page: Playwright 的 Page 实例。
        """
        share_btn = page.locator(
            "button[aria-label*='共享屏幕'], button[aria-label*='Share screen'], "
            "button[aria-label*='屏幕共享'], div[data-testid*='share-screen']"
        ).first
        if not await share_btn.is_visible(timeout=3000):
            msg = "未找到飞书屏幕共享按钮。"
            raise RuntimeError(msg)
        await share_btn.click(timeout=3000)
        await page.wait_for_timeout(1000)

    async def stop_sharing(self, page: Page) -> None:
        """停止在飞书会议中共享屏幕。

        参数:
            page: Playwright 的 Page 实例。
        """
        stop_btn = page.locator(
            "button[aria-label*='停止共享'], button[aria-label*='Stop sharing'], "
            "button:has-text('停止共享'), button:has-text('Stop sharing')"
        ).first
        if not await stop_btn.is_visible(timeout=3000):
            msg = "未找到停止共享按钮。"
            raise RuntimeError(msg)
        await stop_btn.click(timeout=3000)
        await page.wait_for_timeout(500)

    # ── 内部辅助方法 ──────────────────────────────────────────────────

    async def _check_joined(self, page: Page, timeout: float = 20) -> bool:  # noqa: ASYNC109
        """检查是否已成功进入飞书会议室。

        底部工具栏（麦克风按钮、离开按钮等）出现则视为进入成功。

        参数:
            page: Playwright 的 Page 实例。
            timeout: 等待超时时间（秒）。

        返回:
            bool: 已进入会议则为 True，否则为 False。
        """
        # 多个可能的「已进入会议」特征元素
        indicators = [
            page.locator(
                "button[aria-label*='麦克风'], button[aria-label*='Microphone'], "
                "button[aria-label*='Mute'], div[data-testid*='mic']"
            ),
            page.locator(
                "button[aria-label*='离开'], button[aria-label*='Leave'], "
                "div[data-testid*='leave']"
            ),
        ]

        tasks = [
            asyncio.create_task(loc.first.wait_for(state="visible", timeout=0))
            for loc in indicators
        ]
        try:
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED, timeout=timeout
            )
            return any(not t.exception() for t in done)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    async def _open_chat(self, page: Page) -> None:
        """打开飞书会议的聊天面板。

        参数:
            page: Playwright 的 Page 实例。
        """
        # 若聊天输入框已可见则无需重新打开
        chat_visible = await page.locator(
            "div[contenteditable='true'][data-testid*='chat'], "
            "textarea[placeholder*='发送消息'], textarea[placeholder*='Send']"
        ).first.is_visible()
        if chat_visible:
            return

        chat_btn = page.locator(
            "button[aria-label*='聊天'], button[aria-label*='Chat'], "
            "div[data-testid*='chat-btn'], div[data-testid*='chat-panel']"
        ).first
        with contextlib.suppress(Exception):
            await chat_btn.click(timeout=3000)
            await page.wait_for_timeout(800)

    async def _setup_active_speaker_observer(self, page: Page) -> None:
        """为飞书会议注入当前发言人检测逻辑。

        通过 MutationObserver 监听 DOM 变化，将当前高亮的参与者姓名
        写入 ``self._state["active_speaker"]``。

        参数:
            page: Playwright 的 Page 实例。
        """
        with contextlib.suppress(Exception):
            await page.expose_binding(
                "feishuReportSpeaker",
                lambda _, name: self._state.update({"active_speaker": name or None}),
            )
            await page.evaluate(
                """
                () => {
                    const report = n => {
                        if (typeof window.feishuReportSpeaker === 'function') {
                            window.feishuReportSpeaker(n);
                        }
                    };

                    // 飞书会议中高亮发言人通常有特定的 class 或 aria 属性
                    const findSpeaker = () => {
                        // 尝试多种选择器（飞书版本迭代后选择器可能变化）
                        const selectors = [
                            '[data-testid*="active-speaker"] [class*="name"]',
                            '[class*="speaking"] [class*="name"]',
                            '[class*="active"] [class*="member-name"]',
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.textContent.trim()) {
                                return el.textContent.trim();
                            }
                        }
                        return null;
                    };

                    let lastSpeaker = null;
                    new MutationObserver(() => {
                        const cur = findSpeaker();
                        if (cur !== lastSpeaker) {
                            lastSpeaker = cur;
                            report(cur);
                        }
                    }).observe(document.body, {
                        subtree: true,
                        childList: true,
                        attributes: true,
                        attributeFilter: ['class', 'data-speaking', 'aria-label'],
                    });

                    report(findSpeaker());
                }
                """
            )
