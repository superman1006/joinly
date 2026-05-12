import asyncio
import contextlib
import logging
import re
from typing import Any, ClassVar

from playwright.async_api import Page

from joinly.providers.browser.platforms.base import BaseBrowserPlatformController
from joinly.settings import get_settings
from joinly.types import MeetingChatHistory, MeetingChatMessage, MeetingParticipant

logger = logging.getLogger(__name__)

# 飞书视频会议 Web 端的消息最大字符数限制
_MAX_MESSAGE_LENGTH = 1000

# 按钮 accessible-name 匹配正则（中英双语）
_JOIN_RE = re.compile(
    r"加入会议|立即加入|join\s*meeting|join\s*now|enter", re.IGNORECASE
)
_LEAVE_RE = re.compile(r"^(?:离开|结束会议|leave|end)", re.IGNORECASE)
_LEAVE_CONFIRM_RE = re.compile(r"离开|leave|确认|confirm|ok", re.IGNORECASE)
_MIC_RE = re.compile(r"麦克风|mic|mute|microphone", re.IGNORECASE)
_UNMUTE_RE = re.compile(r"开启麦克风|取消静音|unmute|turn on mic", re.IGNORECASE)
_CHAT_RE = re.compile(r"^聊天$|^chat$", re.IGNORECASE)
_MEMBERS_RE = re.compile(r"参会成员|成员|参与者|members|participants", re.IGNORECASE)
_SHARE_RE = re.compile(r"共享屏幕|屏幕共享|share screen|present", re.IGNORECASE)
_STOP_SHARE_RE = re.compile(r"停止共享|stop sharing|stop present", re.IGNORECASE)

# 姓名输入框 placeholder 正则
_NAME_PLACEHOLDER_RE = re.compile(r"名字|name|your name|enter.*name", re.IGNORECASE)

# 密码输入框 placeholder 正则
_PASSCODE_PLACEHOLDER_RE = re.compile(
    r"密码|passcode|password|meeting\s*id", re.IGNORECASE
)


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
        passcode: str | None = None,
    ) -> None:
        """加入飞书视频会议。

        参数:
            page: Playwright 的 Page 实例。
            url: 飞书会议 URL（格式：https://vc.feishu.cn/j/<会议号>）。
            name: 参与者显示名称。
            passcode: 会议密码（若需要）。
        """
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # 等待大厅页面 JS 初始化完成
        await page.wait_for_timeout(3000)

        # ── 填写参与者姓名 ──────────────────────────────────────────────
        # 策略 1：get_by_placeholder（最稳定）
        name_input = page.get_by_placeholder(_NAME_PLACEHOLDER_RE)
        if not await name_input.is_visible(timeout=5000):
            # 策略 2：通用文本输入框
            name_input = page.locator(
                "input[type='text']:not([type='password']):not([type='search'])"
            ).first

        with contextlib.suppress(Exception):
            if await name_input.is_visible(timeout=3000):
                await name_input.triple_click()
                await name_input.fill(name)
                await page.wait_for_timeout(200)
                logger.debug("已填写参与者姓名：%s", name)

        # ── 填写会议密码（若需要）──────────────────────────────────────
        if passcode:
            passcode_input = page.get_by_placeholder(_PASSCODE_PLACEHOLDER_RE)
            if not await passcode_input.is_visible(timeout=2000):
                passcode_input = page.locator("input[type='password']").first
            with contextlib.suppress(Exception):
                if await passcode_input.is_visible(timeout=2000):
                    await passcode_input.fill(passcode)
                    await page.wait_for_timeout(200)
                    logger.debug("已填写会议密码。")

        # ── 点击「加入会议」按钮 ────────────────────────────────────────
        # 策略 1：get_by_role（最稳定，匹配 aria-label 或文字）
        join_btn = page.get_by_role("button", name=_JOIN_RE)
        if not await join_btn.is_visible(timeout=3000):
            # 策略 2：文字匹配（兜底）
            join_btn = page.locator(
                "button:has-text('加入会议'), button:has-text('立即加入'), "
                "button:has-text('Join'), button:has-text('Enter')"
            ).first

        try:
            await join_btn.click(timeout=10000)
            logger.debug("已点击「加入会议」按钮。")
        except Exception as e:
            # 尝试通过 JS 点击可见的「加入」类按钮
            clicked = await self._js_click_join_button(page)
            if not clicked:
                msg = f"未找到「加入会议」按钮或点击失败：{e}"
                logger.exception(msg)
                raise RuntimeError(msg) from e

        # ── 等待成功进入会议 ────────────────────────────────────────────
        if not await self._check_joined(page):
            msg = "加入飞书会议超时：等待底部工具栏出现失败。"
            raise RuntimeError(msg)

        logger.info("已成功加入飞书会议。")
        await self._setup_active_speaker_observer(page)

    async def leave(self, page: Page) -> None:
        """离开飞书视频会议。

        参数:
            page: Playwright 的 Page 实例。
        """
        # 策略 1：get_by_role
        leave_btn = page.get_by_role("button", name=_LEAVE_RE)
        if not await leave_btn.is_visible(timeout=2000):
            # 策略 2：aria-label / text 兜底
            leave_btn = page.locator(
                "button[aria-label*='离开'], button[aria-label*='Leave'], "
                "button[aria-label*='结束'], button:has-text('离开'), "
                "button:has-text('Leave')"
            ).first

        with contextlib.suppress(Exception):
            if await leave_btn.is_visible(timeout=3000):
                await leave_btn.click(timeout=3000)
                await page.wait_for_timeout(500)

                # 处理确认弹窗
                confirm = page.get_by_role("button", name=_LEAVE_CONFIRM_RE)
                if await confirm.is_visible(timeout=2000):
                    await confirm.click(timeout=2000)
                return

        # 兜底：直接导航离开
        logger.warning("未找到离开按钮，直接导航至空白页。")
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

        # 飞书聊天输入框（contenteditable 或 textarea）
        chat_input = page.locator(
            "div[contenteditable='true']:visible, textarea:visible"
        ).last

        if not await chat_input.is_visible(timeout=5000):
            msg = "未找到聊天输入框或输入框不可见。"
            raise RuntimeError(msg)

        await chat_input.click()
        await page.keyboard.type(message)
        await page.wait_for_timeout(200)
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

        # 通过 JS 从 DOM 中提取聊天记录（比 Playwright 选择器更鲁棒）
        raw: list[dict[str, str]] = await page.evaluate(
            """
            () => {
                const results = [];
                // 尝试多种消息容器选择器
                const containers = document.querySelectorAll(
                    '[class*="chat-message"], [class*="message-item"], '
                    + '[data-testid*="chat-message"], [class*="msg-item"]'
                );
                for (const item of containers) {
                    const senderEl = item.querySelector(
                        '[class*="sender"], [class*="username"], '
                        + '[class*="name"]:not([class*="group"])'
                    );
                    const textEl = item.querySelector(
                        '[class*="content"], [class*="text-body"], '
                        + '[class*="message-text"], [class*="msg-content"]'
                    );
                    const sender = senderEl ? senderEl.textContent.trim() : '';
                    const text = textEl ? textEl.textContent.trim() : '';
                    if (text) {
                        results.push({ sender: sender || '', text });
                    }
                }
                return results;
            }
            """
        )

        for item in raw:
            text = item.get("text", "").strip()
            sender = item.get("sender", "").strip() or None
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
        members_btn = page.get_by_role("button", name=_MEMBERS_RE)
        if not await members_btn.is_visible(timeout=2000):
            members_btn = page.locator(
                "button[aria-label*='参会成员'], button[aria-label*='成员'], "
                "button[aria-label*='participants'], button[aria-label*='members'], "
                "button:has-text('成员'), div[data-testid*='member']"
            ).first

        with contextlib.suppress(Exception):
            if await members_btn.is_visible(timeout=3000):
                await members_btn.click(timeout=3000)
                await page.wait_for_timeout(1000)

        # 通过 JS 提取参与者列表
        names: list[str] = await page.evaluate(
            """
            () => {
                const names = [];
                const items = document.querySelectorAll(
                    '[class*="member-item"], [class*="participant-item"], '
                    + '[class*="member-list"] li, [class*="attendee-item"], '
                    + '[data-testid*="member-item"]'
                );
                for (const item of items) {
                    const nameEl = item.querySelector(
                        '[class*="name"], [class*="username"], [class*="display-name"]'
                    );
                    const text = nameEl ? nameEl.textContent.trim() : '';
                    if (text) names.push(text);
                }
                return names;
            }
            """
        )

        return [MeetingParticipant(name=n, infos=[]) for n in names if n]

    async def mute(self, page: Page) -> None:
        """在飞书会议中将自己静音。

        参数:
            page: Playwright 的 Page 实例。
        """
        # 静音 = 点击「关闭麦克风」按钮（当前为开启状态时可见）
        mute_btn = page.get_by_role("button", name=_MIC_RE)
        if await mute_btn.is_visible(timeout=3000):
            await mute_btn.click(timeout=3000)
        else:
            logger.warning("未找到飞书静音按钮。")

    async def unmute(self, page: Page) -> None:
        """在飞书会议中取消静音。

        参数:
            page: Playwright 的 Page 实例。
        """
        unmute_btn = page.get_by_role("button", name=_UNMUTE_RE)
        if not await unmute_btn.is_visible(timeout=1000):
            unmute_btn = page.get_by_role("button", name=_MIC_RE)
        if await unmute_btn.is_visible(timeout=3000):
            await unmute_btn.click(timeout=3000)
        else:
            logger.warning("未找到飞书取消静音按钮。")

    async def share_screen(self, page: Page) -> None:
        """开始在飞书会议中共享屏幕。

        参数:
            page: Playwright 的 Page 实例。
        """
        share_btn = page.get_by_role("button", name=_SHARE_RE)
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
        stop_btn = page.get_by_role("button", name=_STOP_SHARE_RE)
        if not await stop_btn.is_visible(timeout=3000):
            stop_btn = page.locator(
                "button:has-text('停止共享'), button:has-text('Stop sharing')"
            ).first
        if not await stop_btn.is_visible(timeout=3000):
            msg = "未找到停止共享按钮。"
            raise RuntimeError(msg)
        await stop_btn.click(timeout=3000)
        await page.wait_for_timeout(500)

    # ── 内部辅助方法 ──────────────────────────────────────────────────

    async def _check_joined(self, page: Page, timeout: float = 30) -> bool:  # noqa: ASYNC109
        """检查是否已成功进入飞书会议室（等待底部工具栏出现）。

        参数:
            page: Playwright 的 Page 实例。
            timeout: 等待超时时间（秒），默认 30 秒。

        返回:
            bool: 已进入会议则为 True，否则为 False。
        """
        # 等待任意一个工具栏特征元素出现
        indicators = [
            page.get_by_role("button", name=_MIC_RE),
            page.get_by_role("button", name=_LEAVE_RE),
            page.locator(
                "button[aria-label*='麦克风'], button[aria-label*='Microphone'], "
                "button[aria-label*='Mute'], div[data-testid*='mic-btn']"
            ),
            page.locator(
                "button[aria-label*='离开'], button[aria-label*='Leave'], "
                "div[data-testid*='leave-btn']"
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
        # 判断聊天输入框是否已可见
        chat_visible = await page.locator(
            "div[contenteditable='true']:visible, "
            "textarea[placeholder*='发送']:visible, "
            "textarea[placeholder*='Send']:visible"
        ).is_visible()
        if chat_visible:
            return

        # 打开聊天面板
        chat_btn = page.get_by_role("button", name=_CHAT_RE)
        if not await chat_btn.is_visible(timeout=2000):
            chat_btn = page.locator(
                "button[aria-label*='聊天'], button[aria-label*='Chat'], "
                "div[data-testid*='chat']"
            ).first

        with contextlib.suppress(Exception):
            await chat_btn.click(timeout=3000)
            await page.wait_for_timeout(800)

    async def _js_click_join_button(self, page: Page) -> bool:
        """通过 JS 遍历 DOM 点击「加入会议」类按钮（get_by_role 失败时的兜底策略）。

        参数:
            page: Playwright 的 Page 实例。

        返回:
            bool: 成功点击则为 True，否则为 False。
        """
        clicked: bool = await page.evaluate(
            """
            () => {
                const keywords = ['加入会议', '立即加入', '进入会议',
                                  'Join Meeting', 'Join Now', 'Enter'];
                for (const btn of document.querySelectorAll('button')) {
                    const text = (btn.textContent || '').trim();
                    if (keywords.some(k => text.includes(k)) && !btn.disabled) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }
            """
        )
        return clicked

    async def _setup_active_speaker_observer(self, page: Page) -> None:
        """为飞书会议注入当前发言人检测逻辑。

        通过 MutationObserver 监听 DOM 变化，将当前高亮的参与者姓名
        写入 ``self._state["active_speaker"]``。

        参数:
            page: Playwright 的 Page 实例。
        """
        own_name = get_settings().name
        with contextlib.suppress(Exception):
            await page.expose_binding(
                "feishuReportSpeaker",
                lambda _, n: self._state.update({"active_speaker": n or None}),
            )
            await page.evaluate(
                """
                (ownName) => {
                    const report = n => {
                        if (typeof window.feishuReportSpeaker === 'function') {
                            window.feishuReportSpeaker(n);
                        }
                    };

                    const findSpeaker = () => {
                        // 逐步尝试多种发言人高亮选择器
                        const selectors = [
                            '[data-testid*="active-speaker"] [class*="name"]',
                            '[class*="speaking"] [class*="name"]',
                            '[class*="active"][class*="speaker"] [class*="name"]',
                            '[class*="highlight"] [class*="display-name"]',
                            '[class*="active"] [class*="member-name"]',
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            const text = el ? el.textContent.trim() : null;
                            if (text && text !== ownName) return text;
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
                """,
                own_name,
            )
