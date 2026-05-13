import asyncio
import contextlib
import logging
import re
from typing import Any, ClassVar

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from joinly.providers.browser.platforms.base import BaseBrowserPlatformController
from joinly.settings import get_settings
from joinly.types import MeetingChatHistory, MeetingParticipant, ProviderNotSupportedError

logger = logging.getLogger(__name__)

_JOIN_BTN_RE = re.compile(
    r"join|加入|进入|立即加入|加入会议|进入会议|加入音视频|入会",
    re.IGNORECASE,
)
_LEAVE_BTN_RE = re.compile(
    r"leave|离开|结束|离开会议|结束会议|挂断",
    re.IGNORECASE,
)
_MUTE_BTN_RE = re.compile(
    r"^(mute|静音|关闭麦克风|麦克风关闭)",
    re.IGNORECASE,
)
_UNMUTE_BTN_RE = re.compile(
    r"^(unmute|取消静音|开启麦克风|解除静音|麦克风开启)",
    re.IGNORECASE,
)
_CHAT_BTN_RE = re.compile(r"^(chat|聊天)", re.IGNORECASE)
_SHARE_BTN_RE = re.compile(
    r"(share|共享|屏幕共享|共享屏幕)\b",
    re.IGNORECASE,
)
_STOP_SHARE_BTN_RE = re.compile(
    r"(stop\s*(sharing|presenting)|结束共享|停止共享|停止屏幕共享)",
    re.IGNORECASE,
)


class TranssionMeetBrowserPlatformController(BaseBrowserPlatformController):
    """用于管理飞书视频会议（vc.feishu.cn）的浏览器控制器。"""

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
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """加入传神/飞书视频会议。"""
        await self._join_feishu(page, url, name)

        # 等待会议室 UI 加载完成（传神加入后无可靠的确认元素，用固定等待代替）
        await page.wait_for_timeout(5000)

        await self._setup_active_speaker_observer(page)

    async def _join_feishu(
        self,
        page: Page,
        url: str,
        name: str,  # noqa: ARG002
    ) -> None:
        """执行传神/飞书加入会议流程。

        策略：先用 JS 检查按钮的真实类型/href，能直接拿到 URL 就 page.goto()，
        拿不到再尝试点击，并把所有相关信息 dump 到 /tmp 以便诊断。
        """
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=8000)

        await page.screenshot(path="/tmp/transsion_step1.png")
        logger.info("Step1 URL: %s", page.url)

        # 把页面所有按钮/链接信息 dump 出来便于诊断
        await self._dump_clickables(page, "/tmp/transsion_step1_clickables.txt")

        # ── 第一步：找「Join On This Browser」并尝试跳转 ──
        info = await self._find_element_info(
            page,
            r"join on this browser|join.*browser|网页版入会|通过浏览器|在浏览器中加入",
        )
        logger.info("Step1 button info: %s", info)
        await self._navigate_via_element(page, info)

        await page.screenshot(path="/tmp/transsion_step2.png")
        await self._dump_html(page, "/tmp/transsion_step2.html")
        logger.info("Step2 URL: %s", page.url)
        await self._dump_clickables(page, "/tmp/transsion_step2_clickables.txt")

        # ── 第二步：等 Join 按钮变为 enabled 后再点击 ──
        join_pattern = r"^(?:join|加入|进入|立即加入|加入会议|进入会议|入会)$"
        info = await self._wait_for_enabled_button(page, join_pattern, timeout=30)
        if info is None:
            logger.warning("Join button never became enabled within timeout")
            info = await self._find_element_info(page, join_pattern)
        logger.info("Step2 button info (after wait): %s", info)
        await page.screenshot(path="/tmp/transsion_step2_ready.png")
        await self._navigate_via_element(page, info)

        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=8000)

        logger.info("Final URL: %s", page.url)
        await page.screenshot(path="/tmp/transsion_step3.png")
        await self._dump_html(page, "/tmp/transsion_step3.html")

    @staticmethod
    async def _wait_for_enabled_button(
        page: Page, pattern: str, timeout: int = 30
    ) -> dict | None:
        """轮询查找匹配文本且 enabled 的按钮，最长等待 timeout 秒。"""
        for i in range(timeout * 2):
            info = await page.evaluate(
                """(pattern) => {
                    const re = new RegExp(pattern, 'i');
                    const all = [...document.querySelectorAll('a, button, [role="button"]')];
                    const el = all.find(c => {
                        const text = (c.textContent || '').trim();
                        if (!re.test(text)) return false;
                        if (c.disabled) return false;
                        if (c.getAttribute && c.getAttribute('aria-disabled') === 'true') return false;
                        if (c.className && /disabled/i.test(c.className)) return false;
                        return true;
                    });
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    return {
                        found: true,
                        tag: el.tagName,
                        text: (el.textContent || '').trim().substring(0, 100),
                        href: el.tagName === 'A' ? el.href : null,
                        rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    };
                }""",
                pattern,
            )
            if info:
                logger.info("Button became enabled after %.1fs", i * 0.5)
                return info
            await asyncio.sleep(0.5)
        return None

    @staticmethod
    async def _find_element_info(page: Page, pattern: str) -> dict:
        """在页面中查找匹配文本的可点击元素，返回其类型/href/onclick 等信息。"""
        return await page.evaluate(
            """(pattern) => {
                const re = new RegExp(pattern, 'i');
                const all = [...document.querySelectorAll('a, button, [role="button"], div[onclick], span[onclick]')];
                const el = all.find(c => re.test((c.textContent || '').trim()));
                if (!el) {
                    return {found: false, total_clickables: all.length};
                }
                const rect = el.getBoundingClientRect();
                return {
                    found: true,
                    tag: el.tagName,
                    text: (el.textContent || '').trim().substring(0, 100),
                    href: el.tagName === 'A' ? el.href : null,
                    target: el.tagName === 'A' ? el.target : null,
                    hasOnclick: !!el.onclick,
                    role: el.getAttribute('role'),
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                    outerHTMLStart: el.outerHTML.substring(0, 400),
                };
            }""",
            pattern,
        )

    async def _navigate_via_element(self, page: Page, info: dict) -> None:
        """依据元素信息进行跳转。

        策略优先级：
        1. 有 http href 则直接 page.goto()
        2. 拦截 window.open 捕获 URL → page.goto() 到捕获的 URL
        3. 兜底：点击 + 等待 URL 变化
        """
        if not info.get("found"):
            logger.warning("Element not found")
            return

        old_url = page.url

        # 策略 1：直接 href
        href = info.get("href")
        if href and href not in ("", "javascript:void(0)") and href.startswith("http"):
            logger.info("Strategy 1: navigating directly to href: %s", href)
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            return

        # 策略 2：拦截 window.open 抓 URL
        logger.info("Strategy 2: intercepting window.open and clicking")
        await page.evaluate("""() => {
            window.__capturedUrls = [];
            window.open = function(url, target, features) {
                if (url) window.__capturedUrls.push(String(url));
                return { close: () => {}, focus: () => {}, closed: false };
            };
            window.close = function() {};
            // 同时拦截 location.assign / location.replace
            const _assign = window.location.assign;
            const _replace = window.location.replace;
            try {
                window.location.assign = function(u) { window.__capturedUrls.push(String(u)); };
                window.location.replace = function(u) { window.__capturedUrls.push(String(u)); };
            } catch(e) {}
        }""")

        # 通过 JS 触发点击（绕开 Playwright click 的稳定性检查）
        clicked = await page.evaluate(
            """(text) => {
                const all = [...document.querySelectorAll('a, button, [role="button"]')];
                const el = all.find(c => (c.textContent || '').trim().includes(text));
                if (el) { el.click(); return true; }
                return false;
            }""",
            info["text"],
        )
        logger.info("JS click executed: %s", clicked)

        # 等待 1.5 秒让 onclick 执行完
        await asyncio.sleep(1.5)

        # 读取捕获的 URL
        captured = await page.evaluate("() => window.__capturedUrls || []")
        logger.info("Captured URLs from window.open/location: %s", captured)

        if captured:
            target_url = captured[-1]  # 用最后一个（通常是最终目的地）
            logger.info("Navigating page to captured URL: %s", target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            return

        # 策略 3：检查 URL 是否自己变了
        for _ in range(20):
            await asyncio.sleep(0.5)
            if page.url != old_url:
                logger.info("URL changed to: %s", page.url)
                return

        logger.warning("All strategies failed, URL still: %s", page.url)

    @staticmethod
    async def _dump_clickables(page: Page, path: str) -> None:
        """把页面所有可点击元素的文本/href 写到文件，便于诊断。"""
        data = await page.evaluate(
            """() => {
                const els = [...document.querySelectorAll('a, button, [role="button"]')];
                return els.map(e => ({
                    tag: e.tagName,
                    text: (e.textContent || '').trim().substring(0, 80),
                    href: e.tagName === 'A' ? e.href : null,
                    role: e.getAttribute('role'),
                }));
            }"""
        )
        with contextlib.suppress(Exception):
            with open(path, "w", encoding="utf-8") as f:
                for d in data:
                    f.write(f"{d}\n")

    @staticmethod
    async def _dump_html(page: Page, path: str) -> None:
        """把当前页面 HTML 写到文件，便于诊断。"""
        with contextlib.suppress(Exception):
            html = await page.content()
            with open(path, "w", encoding="utf-8") as f:
                f.write(html[:200000])  # 限制大小

    async def _join_gov_feishu(
        self,
        page: Page,
        url: str,
        name: str,
    ) -> None:
        """加入政府版飞书会议（多次重定向，超时更长）。"""
        await page.goto(url, wait_until="load", timeout=60000)

        async def _dismiss_dialog(page: Page) -> None:
            with contextlib.suppress(PlaywrightTimeoutError):
                await page.click('div[role="dialog"] button', timeout=1000)

        async def _click_join_browser(page: Page) -> None:
            with contextlib.suppress(PlaywrightTimeoutError):
                btn_pattern = re.compile(
                    r"join.*browser|continue.*web|浏览器.*加入|在线加入",
                    re.IGNORECASE,
                )
                join_browser_btn = page.get_by_role("button", name=btn_pattern)
                await join_browser_btn.click(timeout=1000)

        dismiss_dialog = asyncio.create_task(_dismiss_dialog(page))
        join_browser = asyncio.create_task(_click_join_browser(page))

        try:
            name_field = page.locator(
                'input[placeholder*="name" i],'
                'input[placeholder*="昵称"],'
                'input[placeholder*="名字"],'
                'input[placeholder*="您的名字"],'
                'input[placeholder*="请输入"],'
                'input[aria-label*="name" i],'
                'input[aria-label*="昵称"]'
            ).first
            await name_field.fill(name, timeout=40000)

            join_btn = page.get_by_role("button", name=_JOIN_BTN_RE)
            await join_btn.click(timeout=10000)

        finally:
            for task in [dismiss_dialog, join_browser]:
                if not task.done():
                    task.cancel()

    async def leave(self, page: Page) -> None:
        """离开飞书视频会议。"""
        leave_btn = page.get_by_role("button", name=_LEAVE_BTN_RE)
        if not await leave_btn.is_visible():
            msg = "Leave button not found or not visible."
            raise RuntimeError(msg)
        await leave_btn.click(timeout=1000)
        await page.wait_for_timeout(500)

        # 飞书可能弹出离开确认框
        with contextlib.suppress(PlaywrightTimeoutError):
            confirm_btn = page.get_by_role("button", name=_LEAVE_BTN_RE)
            await confirm_btn.click(timeout=2000)

    async def send_chat_message(self, page: Page, message: str) -> None:
        """在飞书视频会议中发送聊天消息。"""
        await self._open_chat(page)

        # 飞书聊天输入框是 contenteditable div，不能用 fill()，需 click + press_sequentially
        chat_input = page.locator("div[contenteditable='true']").first
        if not await chat_input.is_visible():
            msg = "Chat input not found or not visible."
            raise RuntimeError(msg)
        await chat_input.click()
        await chat_input.press_sequentially(message)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:
        """获取飞书视频会议的聊天历史。"""
        msg = (
            "Chat history retrieval is not yet supported for Feishu meetings. "
            "Feishu-specific DOM selectors need to be implemented."
        )
        raise ProviderNotSupportedError(msg)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """获取飞书视频会议的参与者列表。"""
        msg = (
            "Participant list retrieval is not yet supported for Feishu meetings. "
            "Feishu-specific DOM selectors need to be implemented."
        )
        raise ProviderNotSupportedError(msg)

    async def mute(self, page: Page) -> None:
        """在飞书视频会议中将自己静音。"""
        mute_btn = page.get_by_role("button", name=_MUTE_BTN_RE)
        if await mute_btn.is_visible():
            await mute_btn.click(timeout=1000)
        elif not await page.get_by_role("button", name=_UNMUTE_BTN_RE).is_visible():
            msg = "Mute button not found or not visible."
            raise RuntimeError(msg)

    async def unmute(self, page: Page) -> None:
        """在飞书视频会议中取消自己静音。"""
        unmute_btn = page.get_by_role("button", name=_UNMUTE_BTN_RE)
        if await unmute_btn.is_visible():
            await unmute_btn.click(timeout=1000)
        elif not await page.get_by_role("button", name=_MUTE_BTN_RE).is_visible():
            msg = "Unmute button not found or not visible."
            raise RuntimeError(msg)

    async def share_screen(self, page: Page) -> None:
        """在飞书视频会议中开始共享屏幕。"""
        share_btn = page.get_by_role("button", name=_SHARE_BTN_RE)
        if not await share_btn.is_visible():
            msg = "Share button not found or not visible."
            raise RuntimeError(msg)
        await share_btn.click(timeout=2000)
        await page.wait_for_timeout(1000)

        # 可能弹出共享选项菜单，选择「屏幕」
        screen_option = page.locator(
            'button:has-text("Screen"), '
            'button:has-text("Entire screen"), '
            'button:has-text("屏幕"), '
            'button:has-text("整个屏幕"), '
            '[role="menuitem"]:has-text("Screen"), '
            '[role="menuitem"]:has-text("屏幕"), '
            '[aria-label*="screen" i][role="button"]'
        ).first
        with contextlib.suppress(PlaywrightTimeoutError):
            await screen_option.wait_for(state="visible", timeout=3000)
            await screen_option.click(timeout=2000)
            await page.wait_for_timeout(1000)

    async def stop_sharing(self, page: Page) -> None:
        """停止在飞书视频会议中共享屏幕。"""
        stop_btn = page.get_by_role("button", name=_STOP_SHARE_BTN_RE)
        if not await stop_btn.first.is_visible():
            msg = "Stop sharing button not found or not visible."
            raise RuntimeError(msg)
        await stop_btn.first.click(timeout=2000)
        await page.wait_for_timeout(500)

    async def _check_joined(self, page: Page, timeout: float = 20) -> bool:  # noqa: ASYNC109
        """检查是否已成功加入飞书视频会议（等待室或已入会均视为成功）。"""
        locators = [
            # 等待室提示（英文 + 中文）
            page.locator("span >> text=/please wait/i"),
            page.locator("span >> text=/will let you in/i"),
            page.locator("span >> text=/等待主持人/"),
            page.locator("span >> text=/等待进入/"),
            page.locator("span >> text=/等待中/"),
            # 已入会标志：离开按钮可见
            page.get_by_role("button", name=_LEAVE_BTN_RE),
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
        """打开飞书视频会议中的聊天面板。"""
        chat_input = page.locator("div[contenteditable='true']").first
        if await chat_input.is_visible():
            return

        chat_button = page.get_by_role("button", name=_CHAT_BTN_RE)
        if not await chat_button.is_visible():
            msg = "Chat button not found or not visible."
            raise RuntimeError(msg)
        await chat_button.click()
        await page.wait_for_timeout(1000)
        if not await chat_input.is_visible():
            await page.wait_for_timeout(2000)

    async def _setup_active_speaker_observer(self, page: Page) -> None:
        """设置当前说话人观察器。注意：此实现依赖飞书页面 DOM 结构，可能需要根据实际调整。"""
        await page.expose_binding(
            "report",
            lambda _, name: self._state.update({"active_speaker": name}),
        )
        # 飞书说话人检测：通过音量动画元素推断当前发言者
        # 如果飞书更新 DOM 结构，需要在浏览器开发者工具中重新定位对应 selector
        await page.evaluate(
            """
            (nameArg) => {
                const emit = n => window.report(n);
                const find = () => {
                    // 尝试通用策略：查找带有"说话中"动效的参与者名字
                    // 飞书使用 data-user-id 或类似属性标识参与者，需根据实际 DOM 调整
                    const speakingEl = document.querySelector(
                        '[class*="speaking" i] [class*="name" i], '  +
                        '[class*="active-speaker" i] [class*="name" i], '  +
                        '[data-speaking="true"] [class*="name" i]'
                    );
                    if (speakingEl) {
                        const name = speakingEl.textContent?.trim();
                        if (name && name.length > 0 && name !== nameArg) return name;
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
                        attributeFilter: ['class', 'data-speaking']
                    }
                );
                emit(find());
            }
            """,
            get_settings().name,
        )