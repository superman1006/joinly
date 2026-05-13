import asyncio
import contextlib
import json
import logging
import os
import re
from typing import Any, ClassVar

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from joinly.providers.browser.platforms.base import BaseBrowserPlatformController
from joinly.settings import get_settings
from joinly.types import MeetingChatHistory, MeetingParticipant

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

# join 按钮文字精确匹配（用于 _wait_for_enabled_button）
_JOIN_PATTERN = r"^(?:join|加入|进入|立即加入|加入会议|进入会议|入会)$"


class FeishuBrowserPlatformController(BaseBrowserPlatformController):
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
        name: str,  # noqa: ARG002
        passcode: str | None = None,  # noqa: ARG002
    ) -> None:
        """加入飞书视频会议。飞书 Web 端无需填写姓名，直接通过浏览器入会。"""
        await self._join_feishu(page, url)

        if not await self._check_joined(page):
            logger.warning("Join check did not detect expected UI; proceeding anyway")

        await self._setup_active_speaker_observer(page)

    async def _join_feishu(self, page: Page, url: str) -> None:
        """执行飞书加入会议的两段式流程。

        飞书会议链接通常先打开一个"选择入会方式"的中转页，再跳到真正的会议页面，
        因此需要两次导航：
        1) 点击"在浏览器中加入"
        2) 等真正的 Join 按钮 enabled 后再点击
        """
        # 注入 JS 伪装成真实 Chrome，避免被飞书/Lark 检测为自动化浏览器
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['zh-CN', 'zh', 'en-US', 'en']
            });
        """)

        # 注入已登录的飞书 Cookie（跳过手机验证）
        cookies_file = os.environ.get("JOINLY_FEISHU_COOKIES_FILE")
        if cookies_file and os.path.exists(cookies_file):  # noqa: PTH110
            try:
                with open(cookies_file) as f:  # noqa: PTH123, ASYNC230
                    cookies: list[dict] = json.load(f)
                await page.context.add_cookies(cookies)
                logger.info("Injected %d Feishu cookies", len(cookies))
            except Exception:  # noqa: BLE001
                logger.warning("Failed to inject Feishu cookies", exc_info=True)

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=8000)

        await page.screenshot(path="/tmp/feishu_step1.png")  # noqa: S108
        logger.info("Step1 URL: %s", page.url)

        # ── 第一步：点击「在浏览器中加入」跳转到会议页 ──
        info = await self._find_element_info(
            page,
            r"join on this browser|join.*browser|网页版入会|通过浏览器|在浏览器中加入",
        )
        logger.info("Step1 button info: %s", info)
        await self._navigate_via_element(page, info)

        await page.screenshot(path="/tmp/feishu_step2.png")  # noqa: S108
        logger.info("Step2 URL: %s", page.url)

        # ── 第二步：填写姓名（有名字输入框时），再等 Join 按钮 enabled ──
        name_input = page.get_by_placeholder(
            re.compile(r"name|姓名|your name", re.IGNORECASE)
        )
        with contextlib.suppress(PlaywrightTimeoutError):
            await name_input.wait_for(state="visible", timeout=3000)
            await name_input.fill(get_settings().name)
            logger.info("Filled name: %s", get_settings().name)

        # 等待 Join 按钮 enabled，然后直接点击（form submit，不走 navigate_via_element）
        join_btn = page.get_by_role("button", name=re.compile(r"^join$", re.IGNORECASE))
        for _ in range(30):
            if await join_btn.is_enabled():
                break
            await asyncio.sleep(1)
        else:
            logger.warning("Join button did not become enabled within 30s")

        # 记录按钮 HTML，便于调试
        btn_html = await page.evaluate(
            """() => {
                const btns = [...document.querySelectorAll('button')];
                const b = btns.find(b => /^join$/i.test(b.textContent.trim()));
                return b ? b.outerHTML.substring(0, 300) : 'not found';
            }"""
        )
        logger.info("Join button HTML: %s", btn_html)

        logger.info("Clicking Join button")
        # force=True 跳过可见性/稳定性检查，确保点击生效
        await join_btn.click(force=True)
        await asyncio.sleep(2)
        # 如果表单还在，再用 JS 直接触发点击
        form_visible = await page.locator("text=Join Meeting").is_visible()
        if form_visible:
            logger.info("Form still visible, retrying with JS click")
            await page.evaluate(
                """() => {
                    const btns = [...document.querySelectorAll('button')];
                    const b = btns.find(b => /^join$/i.test(b.textContent.trim()));
                    if (b) b.click();
                }"""
            )

        # 等待入会表单消失
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.locator("text=Join Meeting").wait_for(
                state="hidden", timeout=30000
            )

        await page.screenshot(path="/tmp/feishu_step3.png")  # noqa: S108
        logger.info("Final URL: %s", page.url)

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

        # 飞书聊天输入框是 contenteditable div，不能用 fill()，需 press_sequentially
        chat_input = page.locator("div[contenteditable='true']").first
        if not await chat_input.is_visible():
            msg = "Chat input not found or not visible."
            raise RuntimeError(msg)
        await chat_input.click()
        await chat_input.press_sequentially(message)
        await page.wait_for_timeout(500)
        await page.keyboard.press("Enter")

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:
        """获取飞书视频会议的聊天历史（暂不支持）。"""
        return await super().get_chat_history(page)

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """获取飞书视频会议的参与者列表（暂不支持）。"""
        return await super().get_participants(page)

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

    # ── 内部辅助方法 ──────────────────────────────────────────────────

    @staticmethod
    async def _wait_for_enabled_button(
        page: Page, pattern: str, max_wait: int = 30
    ) -> dict | None:
        """轮询查找匹配文本且 enabled 的按钮，最长等待 max_wait 秒。"""
        for i in range(max_wait * 2):
            info = await page.evaluate(
                """(pattern) => {
                    const re = new RegExp(pattern, 'i');
                    const all = [
                        ...document.querySelectorAll('a, button, [role="button"]')
                    ];
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
                    const rect = el.getBoundingClientRect();
                    return {
                        found: true,
                        tag: el.tagName,
                        text: (el.textContent || '').trim().substring(0, 100),
                        href: el.tagName === 'A' ? el.href : null,
                        rect: {x:rect.x, y:rect.y, w:rect.width, h:rect.height},
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
                const all = [...document.querySelectorAll(
                    'a, button, [role="button"], div[onclick], span[onclick]'
                )];
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
                    rect: {x:rect.x, y:rect.y, w:rect.width, h:rect.height},
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
        href = info.get("href") or ""
        if href and href not in ("", "javascript:void(0)") and href.startswith("http"):
            logger.info("Strategy 1: navigating directly to href: %s", href)
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            return

        # 策略 2：拦截 window.open 抓 URL
        logger.info("Strategy 2: intercepting window.open and clicking")
        await page.evaluate(
            """() => {
                window.__capturedUrls = [];
                window.open = function(url, target, features) {
                    if (url) window.__capturedUrls.push(String(url));
                    return { close: () => {}, focus: () => {}, closed: false };
                };
                window.close = function() {};
                try {
                    window.location.assign =
                        function(u) { window.__capturedUrls.push(String(u)); };
                    window.location.replace =
                        function(u) { window.__capturedUrls.push(String(u)); };
                } catch(e) {}
            }"""
        )

        # 通过 JS 触发点击（绕开 Playwright click 的稳定性检查）
        text = info.get("text", "")
        clicked = await page.evaluate(
            """(text) => {
                const sel = 'a, button, [role="button"]';
                const all = [...document.querySelectorAll(sel)];
                const el = all.find(
                    c => (c.textContent || '').trim().includes(text)
                );
                if (el) { el.click(); return true; }
                return false;
            }""",
            text,
        )
        logger.info("JS click executed: %s", clicked)

        # 等待 1.5 秒让 onclick 执行完
        await asyncio.sleep(1.5)

        # 读取捕获的 URL
        captured: list[str] = await page.evaluate("() => window.__capturedUrls || []")
        logger.info("Captured URLs from window.open/location: %s", captured)

        if captured:
            target_url = captured[-1]
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
        """设置当前说话人观察器。"""
        own_name = get_settings().name
        await page.expose_binding(
            "report",
            lambda _, name: self._state.update({"active_speaker": name}),
        )
        # 飞书说话人检测：通过音量动画元素推断当前发言者
        await page.evaluate(
            """
            (nameArg) => {
                const emit = n => window.report(n);
                const find = () => {
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
            own_name,
        )
