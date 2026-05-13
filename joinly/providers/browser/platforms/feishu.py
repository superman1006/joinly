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

_MAX_MESSAGE_LENGTH = 1000

# 按钮文字匹配正则（中英双语）
_JOIN_BTN_RE = re.compile(
    r"^(?:join|加入|进入|立即加入|加入会议|进入会议|加入音视频|入会)$",
    re.IGNORECASE,
)
_JOIN_BROWSER_RE = re.compile(
    r"join on this browser|join.*browser|网页版入会|通过浏览器|在浏览器中加入|网页加入",
    re.IGNORECASE,
)
_LEAVE_BTN_RE = re.compile(
    r"^(?:leave|离开|结束|离开会议|结束会议|挂断)$",
    re.IGNORECASE,
)
_LEAVE_CONFIRM_RE = re.compile(r"离开|leave|确认|confirm|ok", re.IGNORECASE)
_MUTE_BTN_RE = re.compile(
    r"^(?:mute|静音|关闭麦克风|麦克风关闭)",
    re.IGNORECASE,
)
_UNMUTE_BTN_RE = re.compile(
    r"^(?:unmute|取消静音|开启麦克风|解除静音|麦克风开启)",
    re.IGNORECASE,
)
_CHAT_BTN_RE = re.compile(r"^(?:chat|聊天)$", re.IGNORECASE)
_MEMBERS_RE = re.compile(
    r"参会成员|成员|参与者|members|participants",
    re.IGNORECASE,
)
_SHARE_BTN_RE = re.compile(
    r"(?:share|共享|屏幕共享|共享屏幕)\b",
    re.IGNORECASE,
)
_STOP_SHARE_BTN_RE = re.compile(
    r"(?:stop\s*(?:sharing|presenting)|结束共享|停止共享|停止屏幕共享)",
    re.IGNORECASE,
)

# 输入框 placeholder 正则
_NAME_PLACEHOLDER_RE = re.compile(
    r"名字|name|your name|enter.*name|请输入|昵称",
    re.IGNORECASE,
)
_PASSCODE_PLACEHOLDER_RE = re.compile(
    r"密码|passcode|password|meeting\s*id",
    re.IGNORECASE,
)

# join 按钮文字匹配（用于 _wait_for_enabled_button）
_JOIN_PATTERN = r"^(?:join|加入|进入|立即加入|加入会议|进入会议|入会)$"


class FeishuBrowserPlatformController(BaseBrowserPlatformController):
    """管理飞书（Lark）浏览器视频会议的控制器。

    支持 vc.feishu.cn 的 /j/ 普通会议和 /live/ 直播房间，
    URL 格式示例：https://vc.feishu.cn/j/905158212
    """

    url_pattern: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?:https?://)?vc\.feishu\.cn/(?:j|live)/[A-Za-z0-9_-]+(?:[/?#].*)?$",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        """初始化飞书浏览器平台控制器。"""
        self._state: dict[str, Any] = {}

    @property
    def active_speaker(self) -> str | None:
        """获取当前说话人的名称。"""
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
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=8000)

        logger.info("Step1 URL: %s", page.url)
        await self._dump_clickables(page, "/tmp/feishu_step1.txt")  # noqa: S108

        # 若存在「在浏览器中加入」中间页，先点击跳转
        join_browser_info = await self._find_element_info(
            page, _JOIN_BROWSER_RE.pattern
        )
        if join_browser_info.get("found"):
            logger.info(
                "Found 'join on browser' button: %s", join_browser_info.get("text")
            )
            await self._navigate_via_element(page, join_browser_info)
            logger.info("Step2 URL: %s", page.url)
            await self._dump_clickables(page, "/tmp/feishu_step2.txt")  # noqa: S108

        # 填写大厅表单（姓名 + 密码）
        await self._fill_lobby_form(page, name, passcode)

        # 等 Join 按钮变为 enabled 后点击
        btn_info = await self._wait_for_enabled_button(page, _JOIN_PATTERN, timeout=30)
        if btn_info:
            logger.info("Join button enabled: %s", btn_info.get("text"))
            await self._navigate_via_element(page, btn_info)
        else:
            await self._click_join_fallback(page)

        # 等待成功进入会议室
        if not await self._check_joined(page):
            await self._dump_html(page, "/tmp/feishu_join_failed.html")  # noqa: S108
            msg = "加入飞书会议超时：等待会议室 UI 出现失败。"
            raise RuntimeError(msg)

        logger.info("已成功加入飞书会议。URL: %s", page.url)
        await self._setup_active_speaker_observer(page)

    async def leave(self, page: Page) -> None:
        """离开飞书视频会议。"""
        leave_btn = page.get_by_role("button", name=_LEAVE_BTN_RE)
        if not await leave_btn.is_visible(timeout=2000):
            leave_btn = page.locator(
                "button[aria-label*='离开'], button[aria-label*='Leave'], "
                "button[aria-label*='结束'], button:has-text('离开'), "
                "button:has-text('Leave'), button:has-text('挂断')"
            ).first

        with contextlib.suppress(Exception):
            if await leave_btn.is_visible(timeout=3000):
                await leave_btn.click(timeout=3000)
                await page.wait_for_timeout(500)
                confirm = page.get_by_role("button", name=_LEAVE_CONFIRM_RE)
                if await confirm.is_visible(timeout=2000):
                    await confirm.click(timeout=2000)
                return

        logger.warning("未找到离开按钮，直接导航至空白页。")
        await page.goto("about:blank")

    async def send_chat_message(self, page: Page, message: str) -> None:
        """在飞书会议中发送聊天消息。"""
        if len(message) > _MAX_MESSAGE_LENGTH:
            msg = (
                f"消息超出最大长度 {_MAX_MESSAGE_LENGTH} 字符，"
                f"当前长度：{len(message)}。"
            )
            raise ValueError(msg)

        await self._open_chat(page)

        # 飞书聊天输入框是 contenteditable div
        chat_input = page.locator("div[contenteditable='true']").first
        if not await chat_input.is_visible(timeout=5000):
            msg = "未找到聊天输入框或输入框不可见。"
            raise RuntimeError(msg)

        await chat_input.click()
        await chat_input.press_sequentially(message)
        await page.wait_for_timeout(300)
        await page.keyboard.press("Enter")

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:
        """获取飞书会议的聊天消息历史。"""
        await self._open_chat(page)
        await page.wait_for_timeout(500)

        raw: list[dict[str, str]] = await page.evaluate(
            """
            () => {
                const results = [];
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
                    if (text) results.push({ sender: sender || '', text });
                }
                return results;
            }
            """
        )

        messages: list[MeetingChatMessage] = []
        for item in raw:
            text = item.get("text", "").strip()
            sender = item.get("sender", "").strip() or None
            if text:
                messages.append(MeetingChatMessage(text=text, sender=sender))
        return MeetingChatHistory(messages=messages)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """获取飞书会议的参与者列表。"""
        members_btn = page.get_by_role("button", name=_MEMBERS_RE)
        if not await members_btn.is_visible(timeout=2000):
            members_btn = page.locator(
                "button[aria-label*='参会成员'], button[aria-label*='成员'], "
                "button[aria-label*='participants'], button[aria-label*='members'], "
                "button:has-text('成员')"
            ).first
        with contextlib.suppress(Exception):
            if await members_btn.is_visible(timeout=3000):
                await members_btn.click(timeout=3000)
                await page.wait_for_timeout(1000)

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
                        '[class*="name"], [class*="username"], '
                        + '[class*="display-name"]'
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
        """在飞书会议中将自己静音。"""
        mute_btn = page.get_by_role("button", name=_MUTE_BTN_RE)
        if await mute_btn.is_visible(timeout=3000):
            await mute_btn.click(timeout=3000)
        elif not await page.get_by_role("button", name=_UNMUTE_BTN_RE).is_visible():
            logger.warning("未找到飞书静音按钮。")

    async def unmute(self, page: Page) -> None:
        """在飞书会议中取消静音。"""
        unmute_btn = page.get_by_role("button", name=_UNMUTE_BTN_RE)
        if await unmute_btn.is_visible(timeout=3000):
            await unmute_btn.click(timeout=3000)
        elif not await page.get_by_role("button", name=_MUTE_BTN_RE).is_visible():
            logger.warning("未找到飞书取消静音按钮。")

    async def share_screen(self, page: Page) -> None:
        """开始在飞书会议中共享屏幕。"""
        share_btn = page.get_by_role("button", name=_SHARE_BTN_RE)
        if not await share_btn.is_visible(timeout=3000):
            msg = "未找到飞书屏幕共享按钮。"
            raise RuntimeError(msg)
        await share_btn.click(timeout=3000)
        await page.wait_for_timeout(1000)

        screen_option = page.locator(
            'button:has-text("Screen"), button:has-text("Entire screen"), '
            'button:has-text("屏幕"), button:has-text("整个屏幕"), '
            '[role="menuitem"]:has-text("Screen"), '
            '[role="menuitem"]:has-text("屏幕")'
        ).first
        with contextlib.suppress(PlaywrightTimeoutError):
            await screen_option.wait_for(state="visible", timeout=3000)
            await screen_option.click(timeout=2000)

    async def stop_sharing(self, page: Page) -> None:
        """停止在飞书会议中共享屏幕。"""
        stop_btn = page.get_by_role("button", name=_STOP_SHARE_BTN_RE)
        if not await stop_btn.first.is_visible(timeout=3000):
            stop_btn = page.locator(
                "button:has-text('停止共享'), button:has-text('Stop sharing')"
            )
        if not await stop_btn.first.is_visible(timeout=3000):
            msg = "未找到停止共享按钮。"
            raise RuntimeError(msg)
        await stop_btn.first.click(timeout=2000)
        await page.wait_for_timeout(500)

    # ── 内部辅助方法 ──────────────────────────────────────────────────

    async def _fill_lobby_form(
        self, page: Page, name: str, passcode: str | None
    ) -> None:
        """填写会议大厅的姓名和密码输入框。"""
        name_input = page.get_by_placeholder(_NAME_PLACEHOLDER_RE)
        if not await name_input.is_visible(timeout=5000):
            name_input = page.locator(
                "input[type='text']:not([type='password']):not([type='search'])"
            ).first
        with contextlib.suppress(Exception):
            if await name_input.is_visible(timeout=3000):
                await name_input.triple_click()
                await name_input.fill(name)
                await page.wait_for_timeout(200)
                logger.debug("已填写参与者姓名：%s", name)

        if not passcode:
            return

        passcode_input = page.get_by_placeholder(_PASSCODE_PLACEHOLDER_RE)
        if not await passcode_input.is_visible(timeout=2000):
            passcode_input = page.locator("input[type='password']").first
        with contextlib.suppress(Exception):
            if await passcode_input.is_visible(timeout=2000):
                await passcode_input.fill(passcode)
                await page.wait_for_timeout(200)
                logger.debug("已填写会议密码。")

    async def _click_join_fallback(self, page: Page) -> None:
        """Join 按钮 enabled 等待超时后的兜底点击策略。"""
        logger.warning("Join button never became enabled, trying fallback.")
        join_btn = page.get_by_role("button", name=_JOIN_BTN_RE)
        try:
            await join_btn.click(timeout=5000)
        except Exception as e:
            clicked = await self._js_click_join_button(page)
            if not clicked:
                msg = f"未找到「加入会议」按钮或点击失败：{e}"
                raise RuntimeError(msg) from e

    async def _check_joined(self, page: Page, timeout: float = 30) -> bool:  # noqa: ASYNC109
        """检查是否已成功进入飞书会议室（含等待室）。"""
        locators = [
            # 等待室提示
            page.locator("span >> text=/please wait/i"),
            page.locator("span >> text=/will let you in/i"),
            page.locator("span >> text=/等待主持人/"),
            page.locator("span >> text=/等待进入/"),
            page.locator("span >> text=/等待中/"),
            # 已入会：离开 / 麦克风按钮可见
            page.get_by_role("button", name=_LEAVE_BTN_RE),
            page.get_by_role("button", name=_MUTE_BTN_RE),
            page.locator(
                "button[aria-label*='麦克风'], button[aria-label*='Microphone'], "
                "button[aria-label*='Mute']"
            ),
        ]
        tasks = [
            asyncio.create_task(loc.first.wait_for(state="visible", timeout=0))
            for loc in locators
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
        """打开飞书会议的聊天面板。"""
        if await page.locator("div[contenteditable='true']:visible").is_visible():
            return
        chat_btn = page.get_by_role("button", name=_CHAT_BTN_RE)
        if not await chat_btn.is_visible(timeout=2000):
            chat_btn = page.locator(
                "button[aria-label*='聊天'], button[aria-label*='Chat']"
            ).first
        with contextlib.suppress(Exception):
            await chat_btn.click(timeout=3000)
            await page.wait_for_timeout(1000)

    @staticmethod
    async def _find_element_info(page: Page, pattern: str) -> dict:
        """在页面中查找匹配文本的可点击元素，返回 tag/href/rect 等信息。"""
        return await page.evaluate(
            """(pattern) => {
                const re = new RegExp(pattern, 'i');
                const all = [...document.querySelectorAll(
                    'a, button, [role="button"], div[onclick], span[onclick]'
                )];
                const el = all.find(c => re.test((c.textContent || '').trim()));
                if (!el) return {found: false, total: all.length};
                const r = el.getBoundingClientRect();
                return {
                    found: true,
                    tag: el.tagName,
                    text: (el.textContent || '').trim().substring(0, 100),
                    href: el.tagName === 'A' ? el.href : null,
                    target: el.tagName === 'A' ? el.target : null,
                    role: el.getAttribute('role'),
                    rect: {x: r.x, y: r.y, width: r.width, height: r.height},
                };
            }""",
            pattern,
        )

    @staticmethod
    async def _wait_for_enabled_button(
        page: Page,
        pattern: str,
        timeout: int = 30,  # noqa: ASYNC109
    ) -> dict | None:
        """轮询等待匹配文本且处于 enabled 状态的按钮（最长 timeout 秒）。"""
        for i in range(timeout * 2):
            info = await page.evaluate(
                """(pattern) => {
                    const re = new RegExp(pattern, 'i');
                    const all = [...document.querySelectorAll(
                        'a, button, [role="button"]'
                    )];
                    const el = all.find(c => {
                        const text = (c.textContent || '').trim();
                        if (!re.test(text)) return false;
                        if (c.disabled) return false;
                        const ad = c.getAttribute && c.getAttribute('aria-disabled');
                        if (ad === 'true') return false;
                        if (c.className && /disabled/i.test(c.className)) return false;
                        return true;
                    });
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {
                        found: true,
                        tag: el.tagName,
                        text: (el.textContent || '').trim().substring(0, 100),
                        href: el.tagName === 'A' ? el.href : null,
                        rect: {x: r.x, y: r.y, width: r.width, height: r.height},
                    };
                }""",
                pattern,
            )
            if info:
                logger.info("Button enabled after %.1fs: %s", i * 0.5, info.get("text"))
                return info
            await asyncio.sleep(0.5)
        return None

    async def _navigate_via_element(self, page: Page, info: dict) -> None:
        """依据元素信息执行跳转或点击。

        优先级：① 直接 href 跳转 → ② 拦截 window.open/location → ③ 等待 URL 变化。
        """
        if not info.get("found"):
            logger.warning("Element not found, skipping navigation.")
            return

        old_url = page.url
        href = info.get("href") or ""

        # 策略 1：有 http href 直接 goto
        if href and href not in ("", "javascript:void(0)") and href.startswith("http"):
            logger.info("Navigate via href: %s", href)
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            return

        # 策略 2：拦截 window.open / location.assign，JS click 触发
        logger.info("Navigate via JS click + window.open intercept")
        await page.evaluate(
            """() => {
                window.__capturedUrls = [];
                window.open = function(url) {
                    if (url) window.__capturedUrls.push(String(url));
                    return {close:()=>{}, focus:()=>{}, closed:false};
                };
                try {
                    window.location.assign =
                        u => window.__capturedUrls.push(String(u));
                    window.location.replace =
                        u => window.__capturedUrls.push(String(u));
                } catch(e) {}
            }"""
        )
        text = info.get("text", "")
        await page.evaluate(
            """(text) => {
                const all = [...document.querySelectorAll(
                    'a, button, [role="button"]'
                )];
                const el = all.find(
                    c => (c.textContent || '').trim().includes(text)
                );
                if (el) el.click();
            }""",
            text,
        )
        await asyncio.sleep(1.5)

        captured: list[str] = await page.evaluate("() => window.__capturedUrls || []")
        if captured:
            target = captured[-1]
            logger.info("Navigating to captured URL: %s", target)
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            return

        # 策略 3：等待 URL 自动跳转
        for _ in range(20):
            await asyncio.sleep(0.5)
            if page.url != old_url:
                logger.info("URL changed to: %s", page.url)
                return

        logger.warning("All navigation strategies done, URL: %s", page.url)

    async def _js_click_join_button(self, page: Page) -> bool:
        """通过 JS 遍历按钮找「加入会议」类文字并点击（兜底策略）。"""
        return await page.evaluate(
            """() => {
                const kw = [
                    '加入会议','立即加入','进入会议','加入音视频','入会',
                    'Join Meeting','Join Now','Join'
                ];
                for (const btn of document.querySelectorAll('button')) {
                    const text = (btn.textContent || '').trim();
                    if (kw.some(k => text.includes(k)) && !btn.disabled) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }"""
        )

    @staticmethod
    async def _dump_clickables(page: Page, path: str) -> None:
        """将页面所有可点击元素的文本/href 写入文件（调试用）。"""
        with contextlib.suppress(Exception):
            data = await page.evaluate(
                """() => [...document.querySelectorAll(
                    'a, button, [role="button"]'
                )].map(e => ({
                    tag: e.tagName,
                    text: (e.textContent || '').trim().substring(0, 80),
                    href: e.tagName === 'A' ? e.href : null,
                    disabled: e.disabled || false,
                }))"""
            )
            await asyncio.to_thread(_write_lines, path, [str(d) for d in data])

    @staticmethod
    async def _dump_html(page: Page, path: str) -> None:
        """将当前页面 HTML 写入文件（调试用）。"""
        with contextlib.suppress(Exception):
            html = await page.content()
            await asyncio.to_thread(_write_text, path, html[:200000])

    async def _setup_active_speaker_observer(self, page: Page) -> None:
        """为飞书会议注入当前发言人检测逻辑。"""
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
                        if (typeof window.feishuReportSpeaker === 'function')
                            window.feishuReportSpeaker(n);
                    };
                    const findSpeaker = () => {
                        const selectors = [
                            '[data-testid*="active-speaker"] [class*="name"]',
                            '[class*="speaking"] [class*="name"]',
                            '[class*="active-speaker"] [class*="name"]',
                            '[data-speaking="true"] [class*="name"]',
                            '[class*="highlight"] [class*="display-name"]',
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            const text = el ? el.textContent.trim() : null;
                            if (text && text !== ownName) return text;
                        }
                        return null;
                    };
                    let last = null;
                    new MutationObserver(() => {
                        const cur = findSpeaker();
                        if (cur !== last) { last = cur; report(cur); }
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


def _write_lines(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:  # noqa: PTH123
        f.writelines(line + "\n" for line in lines)


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:  # noqa: PTH123
        f.write(text)
