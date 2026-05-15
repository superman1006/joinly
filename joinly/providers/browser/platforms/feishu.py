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
from joinly.types import MeetingChatHistory, MeetingChatMessage, MeetingParticipant

logger = logging.getLogger(__name__)

_JOIN_BTN_RE = re.compile(
    r"join|加入|进入|立即加入|加入会议|进入会议|加入音视频|入会",
    re.IGNORECASE,
)
_LEAVE_BTN_RE = re.compile(
    r"leave|离开|结束|离开会议|结束会议|挂断",
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
            logger.warning("入会检查未检测到预期界面，继续运行")

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
                cookies = self._load_cookies(cookies_file)
                await page.context.add_cookies(cookies)
                logger.info("已注入 %d 条飞书 Cookie", len(cookies))
            except Exception:  # noqa: BLE001
                logger.warning("飞书 Cookie 注入失败", exc_info=True)

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state("networkidle", timeout=8000)

        await page.screenshot(path="/tmp/feishu_step1.png")  # noqa: S108
        logger.info("第一步页面 URL: %s", page.url)

        # ── 第一步：等待「在浏览器中加入」按钮出现，再点击 ──
        # 飞书会议加入弹窗由 React 异步渲染，networkidle 后仍可能延迟出现，最多等 20s
        _BROWSER_JOIN_RE = r"join on this browser|join.*browser|网页版入会|通过浏览器|在浏览器中加入"
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_function(
                """(pat) => {
                    const re = new RegExp(pat, 'i');
                    return [...document.querySelectorAll('a, button, [role="button"], div, span')]
                        .some(el => {
                            const t = (el.textContent || '').trim();
                            return t.length < 50 && re.test(t);
                        });
                }""",
                arg=_BROWSER_JOIN_RE,
                timeout=20000,
            )

        # 优先精准点击：飞书 landing page 的「Join On This Browser」按钮带 class="join-meeting"
        # 父容器同时包含「Download Feishu」按钮，文字会被拼接，无法用容器 textContent 匹配
        old_url = page.url
        clicked = await page.evaluate("""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            // 1) 精确 class 匹配
            let btn = [...document.querySelectorAll('button.join-meeting')].find(visible);
            // 2) 兜底：文字精确匹配（仅子按钮，不含父容器）
            if (!btn) {
                const re = /^(?:join on this browser|join.*browser|网页版入会|通过浏览器|在浏览器中加入)$/i;
                btn = [...document.querySelectorAll('button, a, [role="button"]')]
                    .find(el => visible(el) && re.test((el.textContent || '').trim()));
            }
            if (btn) {
                btn.click();
                return {clicked: true, html: btn.outerHTML.substring(0, 200)};
            }
            return {clicked: false};
        }""")
        logger.info("第一步直接点击结果: %s", clicked)

        if clicked.get("clicked"):
            # 等 URL 变化或新内容出现，最多 8 秒
            for _ in range(16):
                if page.url != old_url:
                    break
                await asyncio.sleep(0.5)
            logger.info("直接点击后 URL 变为: %s", page.url)
        else:
            # 兜底：走原来的 _navigate_via_element 流程
            info = await self._find_element_info(page, _BROWSER_JOIN_RE)
            logger.info("第一步兜底查找结果: %s", info)
            if not info.get("found"):
                dump = await page.evaluate("""() => {
                    const els = [...document.querySelectorAll('a, button, [role="button"]')];
                    return els.map(el => ({
                        tag: el.tagName,
                        text: (el.textContent || '').trim().substring(0, 80),
                        visible: el.getBoundingClientRect().width > 0,
                    })).filter(e => e.visible && e.text);
                }""")
                logger.info("第一步可点击元素列表: %s", dump)
            await self._navigate_via_element(page, info)

        await page.screenshot(path="/tmp/feishu_step2.png")  # noqa: S108
        logger.info("第二步页面 URL: %s", page.url)

        # ── 调试：打印页面上所有 input 的 placeholder，帮助定位名字输入框 ──
        inputs_info = await page.evaluate("""() => {
            return [...document.querySelectorAll('input, textarea')].map(el => ({
                tag: el.tagName,
                placeholder: el.placeholder,
                type: el.type,
                visible: el.offsetParent !== null,
            }));
        }""")
        logger.info("页面输入框列表: %s", inputs_info)

        # ── 第二步：填写姓名（有名字输入框时），再等 Join 按钮 enabled ──
        name_input = page.get_by_placeholder(
            re.compile(r"name|姓名|your name", re.IGNORECASE)
        )
        name_filled = False
        try:
            await name_input.wait_for(state="visible", timeout=10000)
            await name_input.fill(get_settings().name)
            name_filled = True
            logger.info("已填写姓名: %s", get_settings().name)
        except PlaywrightTimeoutError:
            logger.warning("10 秒内未找到姓名输入框，继续流程")

        # 检查是否已经进入会议（已登录用户可能跳过 join 表单直接入会）
        already_joined = await self._check_joined(page, timeout=3)
        if already_joined:
            logger.info("第一步后已在会议中，跳过加入按钮")
            return

        # 等待 Join 按钮 enabled，然后直接点击（form submit，不走 navigate_via_element）
        # 注意：已登录用户无需填名字，按钮会在页面加载完成后自动变可用，需等待
        join_btn = page.get_by_role(
            "button",
            name=re.compile(r"^(?:join|加入|进入|立即加入|加入会议|进入会议|入会)$", re.IGNORECASE),
        )
        for _ in range(30):
            if await join_btn.is_enabled():
                break
            await asyncio.sleep(1)
        else:
            logger.warning("加入按钮 30 秒内未变为可用状态")

        # 记录按钮 HTML，便于调试
        btn_html = await page.evaluate(
            """() => {
                const btns = [...document.querySelectorAll('button')];
                const b = btns.find(b => /^(?:join|加入|进入|立即加入|加入会议|进入会议|入会)$/i.test(b.textContent.trim()));
                return b ? b.outerHTML.substring(0, 300) : 'not found';
            }"""
        )
        logger.info("加入按钮 HTML: %s", btn_html)

        logger.info("正在点击加入按钮")
        # force=True 跳过可见性/稳定性检查，确保点击生效
        await join_btn.click(force=True)
        await asyncio.sleep(2)
        # 如果表单还在，再用 JS 直接触发点击
        form_visible = await page.locator("text=Join Meeting").is_visible()
        if form_visible:
            logger.info("表单仍可见，改用 JS 点击重试")
            await page.evaluate(
                """() => {
                    const btns = [...document.querySelectorAll('button')];
                    const b = btns.find(b =>
                        /^(?:join|加入|进入|立即加入|加入会议|进入会议|入会)$/i.test(b.textContent.trim()));
                    if (b) b.click();
                }"""
            )

        # 等待入会表单消失
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.locator("text=Join Meeting").wait_for(
                state="hidden", timeout=30000
            )

        await page.screenshot(path="/tmp/feishu_step3.png")  # noqa: S108
        logger.info("最终页面 URL: %s", page.url)

    async def leave(self, page: Page) -> None:
        """离开飞书视频会议（点击红色挂断按钮）。"""
        # 用 JS 直接点击可见的挂断按钮，绕开 Playwright 可见性检查
        clicked = await page.evaluate("""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const btns = [...document.querySelectorAll('button')];
            const target = btns.find(b =>
                visible(b) && b.querySelector('svg[data-icon="CallEndFilled"]'));
            if (target) { target.click(); return true; }
            return false;
        }""")
        if not clicked:
            msg = "未找到离开按钮"
            raise RuntimeError(msg)
        await page.wait_for_timeout(500)

        # 离开确认框（如果出现）
        with contextlib.suppress(PlaywrightTimeoutError):
            confirm_btn = page.get_by_role("button", name=_LEAVE_BTN_RE)
            await confirm_btn.click(timeout=2000)

    async def send_chat_message(self, page: Page, message: str) -> None:
        """在飞书视频会议中发送聊天消息。

        飞书的 lark-editor 是富文本编辑器（pre.lark-editor[contenteditable=true]），
        内容包裹在 <p> 里。普通的 press_sequentially 触发的 keyDown/keyUp 不一定被识别，
        改用 keyboard.insertText 模拟 IME 输入（更兼容富文本）。
        """
        await self._open_chat(page)

        chat_input = await self._find_chat_input(page)
        if chat_input is None:
            msg = "未找到聊天输入框或输入框不可见"
            raise RuntimeError(msg)

        # 先聚焦 lark-editor 并清空已有内容（防止上次残留）
        await chat_input.click()
        await page.wait_for_timeout(200)
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(100)

        # 用 insertText 一次性写入（IME 风格），触发编辑器的 composition events
        await page.keyboard.insert_text(message)
        await page.wait_for_timeout(500)

        # 验证文本已输入
        typed = await page.evaluate("""() => {
            const e = document.querySelector('pre.lark-editor');
            return e ? e.textContent.trim() : '';
        }""")
        logger.info("发送前编辑器内容: %r", typed)

        # 直接对 lark-editor 元素按 Enter（不是 page 级），确保 Enter 落到编辑器上
        await chat_input.press("Enter")
        await page.wait_for_timeout(500)

        remaining = await page.evaluate("""() => {
            const e = document.querySelector('pre.lark-editor');
            return e ? e.textContent.trim() : '';
        }""")

        # 如果 Enter 没清空编辑器，用 JS 派发完整的 keyboard 事件序列（绕开 Playwright）
        if remaining:
            logger.warning("通过定位器按 Enter 失败（内容仍为: %r），改用 JS 事件派发", remaining)
            sent = await page.evaluate("""() => {
                const e = document.querySelector('pre.lark-editor');
                if (!e) return false;
                e.focus();
                const fire = type => e.dispatchEvent(new KeyboardEvent(type, {
                    key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
                    bubbles: true, cancelable: true, composed: true
                }));
                fire('keydown');
                fire('keypress');
                fire('keyup');
                return true;
            }""")
            await page.wait_for_timeout(500)
            remaining2 = await page.evaluate("""() => {
                const e = document.querySelector('pre.lark-editor');
                return e ? e.textContent.trim() : '';
            }""")
            logger.info("JS Enter 事件派发后: sent=%s，编辑器内容=%r", sent, remaining2)

        logger.info("聊天消息已发送: %s", message)

    async def get_chat_history(self, page: Page) -> MeetingChatHistory:  # noqa: ARG002
        """获取飞书视频会议的聊天历史（暂不支持）。"""
        return MeetingChatHistory(
            messages=[MeetingChatMessage(text="没有查到", sender="system")]
        )

    async def get_participants(self, page: Page) -> list[MeetingParticipant]:
        """获取飞书视频会议的参与者列表。

        直接从视频 tile 的名字标签（span.q4OxoBwf）读取，无需展开参会人面板。
        """
        # 完整 dump：节点数 + 原始文本 + 是否可见
        dump = await page.evaluate("""() => {
            const els = [...document.querySelectorAll('.q4OxoBwf')];
            return {
                count: els.length,
                items: els.map(el => ({
                    text: el.textContent?.trim() || '',
                    visible: el.getBoundingClientRect().width > 0,
                    parentTag: el.parentElement?.tagName || null,
                })),
            };
        }""")
        logger.info("参会人 DOM 快照: %s", dump)

        raw_names = [item["text"] for item in dump["items"] if item["text"]]

        seen: set[str] = set()
        participants = []
        for name in raw_names:
            clean = re.sub(r'\s*\([Mm]e\)\s*$|\s*（我）\s*$|\s*\(我\)\s*$', '', name).strip()
            if clean and clean not in seen:
                seen.add(clean)
                participants.append(MeetingParticipant(name=clean))

        logger.info("参会人列表: %s", [p.name for p in participants])
        return participants or [MeetingParticipant(name="没有查到")]

    async def send_reaction(self, page: Page, name: str = "thumbsup") -> None:
        """发送弹幕表情（左下角 3 个快捷表情）。

        参数:
            name: 表情名称，支持：
                - "thumbsup" / "赞" / "👍"  → 大拇指
                - "ok"                       → OK
                - "plusone" / "+1"           → +1
        """
        # 三个弹幕表情的 alt 属性映射
        alt_map = {
            "thumbsup": ["thumbsup_v2", "赞", "👍"],
            "ok": ["ok_v2", "OK"],
            "plusone": ["plusone", "+1"],
        }
        key = name.lower().strip()
        # 反查规范化名
        canonical = next(
            (k for k, aliases in alt_map.items()
             if key == k or key in [a.lower() for a in aliases]),
            None,
        )
        if canonical is None:
            msg = f"未知表情: {name}，支持的表情: thumbsup、ok、plusone"
            raise ValueError(msg)

        # 通过 img class 精准定位，点击其父级 span wrapper（span.larkw-emoji__wrapper）
        cls_suffix = {"thumbsup": "thumbsup_v2", "ok": "ok_v2", "plusone": "plusone"}[canonical]
        emoji_img = page.locator(f'img.larkw-emoji__img--emoji-{cls_suffix}')
        if await emoji_img.count() == 0:
            msg = f"未找到表情按钮 '{canonical}'"
            raise RuntimeError(msg)
        # img → span.larkw-emoji__wrapper → div.OPdv4wQt，点 wrapper span
        await emoji_img.first.locator("xpath=ancestor::span[contains(@class,'larkw-emoji__wrapper')]").click(timeout=2000)
        logger.info("表情已发送: %s", canonical)

    async def toggle_emoji_panel(self, page: Page) -> None:
        """打开/关闭表情面板（左下角第 4 个按钮，data-icon="EmojiFilled"）。"""
        clicked = await page.evaluate("""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const btns = [...document.querySelectorAll('button')];
            const target = btns.find(b =>
                visible(b) && b.querySelector('svg[data-icon="EmojiFilled"]'));
            if (target) { target.click(); return true; }
            return false;
        }""")
        if not clicked:
            msg = "未找到表情面板按钮"
            raise RuntimeError(msg)

    async def toggle_participants_panel(self, page: Page) -> None:
        """打开/关闭参会人面板（data-icon="GroupFilled"）。"""
        clicked = await page.evaluate("""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const btns = [...document.querySelectorAll('button')];
            const target = btns.find(b =>
                visible(b) && b.querySelector('svg[data-icon="GroupFilled"]'));
            if (target) { target.click(); return true; }
            return false;
        }""")
        if not clicked:
            msg = "未找到参会人面板按钮"
            raise RuntimeError(msg)

    async def mute(self, page: Page) -> None:
        """在飞书视频会议中将自己静音。"""
        # 用 JS 找到「可见的」工具栏麦克风按钮并点击
        # 原因：DOM 中有 5 个 MicOffFilled 按钮（视频 tile / 多套工具栏），
        # Playwright .first 容易选到隐藏的，因此用 getBoundingClientRect 过滤
        result = await page.evaluate("""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const buttons = [...document.querySelectorAll('button.kes3qNGU')];
            const onBtn = buttons.find(b =>
                visible(b) && b.querySelector('svg[data-icon="MicFilled"]'));
            if (onBtn) { onBtn.click(); return 'muted'; }
            const offBtn = buttons.find(b =>
                visible(b) && b.querySelector('svg[data-icon="MicOffFilled"]'));
            if (offBtn) return 'already_muted';
            return 'not_found';
        }""")
        if result == "not_found":
            msg = "未找到静音按钮"
            raise RuntimeError(msg)
        logger.info("静音操作结果: %s", result)

    async def unmute(self, page: Page) -> None:
        """在飞书视频会议中取消自己静音。"""
        result = await page.evaluate("""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const buttons = [...document.querySelectorAll('button.kes3qNGU')];
            const offBtn = buttons.find(b =>
                visible(b) && b.querySelector('svg[data-icon="MicOffFilled"]'));
            if (offBtn) { offBtn.click(); return 'unmuted'; }
            const onBtn = buttons.find(b =>
                visible(b) && b.querySelector('svg[data-icon="MicFilled"]'));
            if (onBtn) return 'already_unmuted';
            return 'not_found';
        }""")
        if result == "not_found":
            msg = "未找到取消静音按钮"
            raise RuntimeError(msg)
        logger.info("取消静音操作结果: %s", result)

    async def share_screen(self, page: Page) -> None:
        """在飞书视频会议中开始共享屏幕。"""
        share_btn = page.get_by_role("button", name=_SHARE_BTN_RE)
        if not await share_btn.is_visible():
            msg = "未找到共享屏幕按钮或按钮不可见"
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
            msg = "未找到停止共享按钮或按钮不可见"
            raise RuntimeError(msg)
        await stop_btn.first.click(timeout=2000)
        await page.wait_for_timeout(500)

    # ── 内部辅助方法 ──────────────────────────────────────────────────

    @staticmethod
    def _load_cookies(cookies_file: str) -> list[dict]:
        """从文件加载 Cookie，并规范化 sameSite 字段以兼容 Playwright。"""
        _same_site_map = {
            "no_restriction": "None",
            "lax": "Lax",
            "strict": "Strict",
            "none": "None",
            "unspecified": "Lax",
        }
        with open(cookies_file) as f:  # noqa: PTH123
            cookies: list[dict] = json.load(f)
        for c in cookies:
            raw = str(c.get("sameSite", "")).lower()
            c["sameSite"] = _same_site_map.get(raw, "Lax")
        return cookies

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
                logger.info("按钮在 %.1f 秒后变为可用状态", i * 0.5)
                return info
            await asyncio.sleep(0.5)
        return None

    @staticmethod
    async def _find_element_info(page: Page, pattern: str) -> dict:
        """在页面中查找匹配文本的可点击元素，返回其类型/href/onclick 等信息。"""
        return await page.evaluate(
            """(pattern) => {
                const re = new RegExp(pattern, 'i');
                // 先查标准可点击元素（button/a/role=button），再扩展到所有 div/span
                // React 组件的点击事件不写在 onclick 属性上，所以不能只查 [onclick]
                const all = [...document.querySelectorAll(
                    'a, button, [role="button"], div, span'
                )];
                const el = all.find(c => {
                    const text = (c.textContent || '').trim();
                    // 只匹配文字内容较短的元素，避免匹配整个页面容器
                    return text.length < 50 && re.test(text);
                });
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
            logger.warning("未找到目标元素")
            return

        old_url = page.url

        # 策略 1：直接 href
        href = info.get("href") or ""
        if href and href not in ("", "javascript:void(0)") and href.startswith("http"):
            logger.info("策略 1：直接跳转到 href: %s", href)
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            return

        # 策略 2：拦截 window.open 抓 URL
        logger.info("策略 2：拦截 window.open 并点击")
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
        logger.info("JS 点击已执行: %s", clicked)

        # 等待 1.5 秒让 onclick 执行完
        await asyncio.sleep(1.5)

        # 读取捕获的 URL
        captured: list[str] = await page.evaluate("() => window.__capturedUrls || []")
        logger.info("从 window.open/location 捕获的 URL: %s", captured)

        if captured:
            target_url = captured[-1]
            logger.info("导航至捕获的 URL: %s", target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            return

        # 策略 3：检查 URL 是否自己变了
        for _ in range(20):
            await asyncio.sleep(0.5)
            if page.url != old_url:
                logger.info("URL 已变更为: %s", page.url)
                return

        logger.warning("所有策略均失败，URL 仍为: %s", page.url)

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
        # 先快速判断：聊天面板是否已经打开（多种可能的输入框选择器）
        if await self._find_chat_input(page) is not None:
            return

        # 用 JS 找可见的聊天按钮并点击
        clicked = await page.evaluate("""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const btns = [...document.querySelectorAll('button')];
            const target = btns.find(b =>
                visible(b) && b.querySelector('svg[data-icon="ChatFilled"]'));
            if (target) { target.click(); return true; }
            return false;
        }""")
        if not clicked:
            msg = "未找到聊天按钮或按钮不可见"
            raise RuntimeError(msg)

        # 等待面板动画 + 输入框出现（最多 7 秒，每 500ms 检查一次）
        for _ in range(14):
            await asyncio.sleep(0.5)
            if await self._find_chat_input(page) is not None:
                return

        # 没找到，dump 调试信息
        debug = await page.evaluate("""() => {
            const visible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const candidates = [
                ...document.querySelectorAll(
                    'input, textarea, [contenteditable], [role="textbox"]'
                )
            ].filter(visible);
            return candidates.slice(0, 10).map(el => ({
                tag: el.tagName,
                type: el.type || null,
                role: el.getAttribute('role'),
                contenteditable: el.getAttribute('contenteditable'),
                placeholder: el.placeholder || el.getAttribute('data-placeholder') || el.getAttribute('aria-label'),
                cls: (el.className || '').substring(0, 100),
            }));
        }""")
        logger.error("未找到聊天输入框，可见候选输入元素: %s", debug)
        msg = "打开聊天面板后仍未出现输入框"
        raise RuntimeError(msg)

    @staticmethod
    async def _find_chat_input(page: Page):
        """尝试多种选择器找到可见的聊天输入框，返回 Locator 或 None。

        飞书的实际输入框是 <pre class="lark-editor" contenteditable="true">，
        所以不限定 tag，用 [contenteditable] 通用匹配。
        """
        selectors = [
            "pre.lark-editor",
            "[contenteditable='true']",
            "[contenteditable='plaintext-only']",
            "[role='textbox']",
            "textarea",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible(timeout=200):
                    return loc
            except PlaywrightTimeoutError:
                continue
        return None

    async def _setup_active_speaker_observer(self, page: Page) -> None:
        """设置当前说话人观察器。"""
        own_name = get_settings().name
        await page.expose_binding(
            "report",
            lambda _, name: self._state.update({"active_speaker": name}),
        )
        # 飞书说话人检测：
        # 每个视频 tile 外层容器为 div.GdbRqxLG，说话时该 div 会出现高亮边框。
        # 名字标签在 tile 内的 span.q4OxoBwf，格式如「陈律 (我)」或「陈律」。
        await page.evaluate(
            """
            (ownName) => {
                const emit = n => window.report(n);

                const cleanName = raw => raw
                    .replace(/\\s*\\([Mm]e\\)\\s*$/, '')
                    .replace(/\\s*（我）\\s*$/, '')
                    .replace(/\\s*\\(我\\)\\s*$/, '')
                    .trim();

                const find = () => {
                    const tiles = [...document.querySelectorAll('.GdbRqxLG')];
                    for (const tile of tiles) {
                        const style = window.getComputedStyle(tile);
                        // 说话时 tile 出现非透明边框
                        const hasBorder = style.borderWidth !== '0px'
                            && style.borderColor !== 'rgba(0, 0, 0, 0)'
                            && style.borderColor !== 'transparent';
                        if (!hasBorder) continue;
                        const nameEl = tile.querySelector('.q4OxoBwf');
                        if (!nameEl) continue;
                        const name = cleanName(nameEl.textContent?.trim() || '');
                        if (name && name !== ownName) return name;
                    }
                    return null;
                };

                let last = null;
                new MutationObserver(() => {
                    const cur = find();
                    if (cur !== last) { last = cur; emit(cur); }
                }).observe(document, {
                    subtree: true,
                    childList: true,
                    attributes: true,
                    attributeFilter: ['class', 'style']
                });
                emit(find());
            }
            """,
            own_name,
        )

    async def _dump_participant_dom(self, page: Page) -> None:
        """入会后 dump 工具栏按钮和参与者面板 DOM，帮助定位正确的 CSS 选择器。"""
        await asyncio.sleep(3)  # 等待会议 UI 完全渲染

        # Step1：dump 所有按钮文字，找到"成员/参与者"按钮
        buttons = await page.evaluate("""() => {
            return [...document.querySelectorAll('button, [role="button"]')].map(el => ({
                text: el.textContent?.trim().substring(0, 60),
                ariaLabel: el.getAttribute('aria-label'),
                className: el.className?.substring(0, 80),
            })).filter(b => b.text || b.ariaLabel);
        }""")
        logger.info("所有按钮（共 %d 个）:", len(buttons))
        for b in buttons:
            logger.info("  text=%r aria=%r class=%s", b["text"], b["ariaLabel"], b["className"][:50])

        # Step2：点击数字按钮（飞书用参会人数作为按钮文字）展开参与者面板
        opened = await page.evaluate("""() => {
            const btn = [...document.querySelectorAll('button, [role="button"]')]
                .find(el => /^\\d+$/.test((el.textContent || '').trim()));
            if (btn) { btn.click(); return btn.textContent?.trim(); }
            return null;
        }""")
        logger.info("已点击参会人数量按钮: %r", opened)
        await asyncio.sleep(2)

        # Step3：dump 展开后页面中所有包含多个子元素的列表容器
        panel = await page.evaluate("""() => {
            const lists = [...document.querySelectorAll('ul, ol, [class*="list"], [class*="panel"], [class*="member"], [class*="roster"]')]
                .filter(el => el.children.length >= 1);
            return lists.slice(0, 5).map(el => ({
                tag: el.tagName,
                cls: el.className?.substring(0, 100),
                childCount: el.children.length,
                childrenText: [...el.children].slice(0, 10).map(c => ({
                    tag: c.tagName,
                    cls: c.className?.substring(0, 80),
                    text: c.textContent?.trim().substring(0, 60),
                })),
            }));
        }""")
        logger.info("参会人面板（共 %d 个列表容器）:", len(panel))
        for container in panel:
            logger.info("  [%s.%s] %d children:", container["tag"], container["cls"][:60], container["childCount"])
            for child in container.get("childrenText", []):
                logger.info("    [%s.%s] %r", child["tag"], child["cls"][:50], child["text"])
