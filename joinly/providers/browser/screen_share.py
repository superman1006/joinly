"""通过画布叠加与标签页自采集实现屏幕共享。

Injects a full-screen ``<canvas>`` on the meeting tab that receives CDP
screencast frames from a separate content tab.  A ``getDisplayMedia``
override uses tab self-capture so the platform receives a real
browser-produced stream containing the canvas content.

The canvas sits at ``z-index:999999`` with ``pointer-events:none`` so
Playwright automation on the meeting page still works (clicks pass
through to the DOM underneath).
"""

from playwright.async_api import Page

_INSTALL_OVERLAY_JS = """\
({ w, h }) => {
    // --- canvas overlay (re-created on every share) ---
    let c = document.getElementById('__scOverlay');
    if (!c) {
        c = document.createElement('canvas');
        c.id = '__scOverlay';
        document.body.appendChild(c);
    }
    c.width = w; c.height = h;
    c.style.cssText = [
        'position:fixed', 'inset:0',
        'width:100vw', 'height:100vh',
        'z-index:999999', 'pointer-events:none',
    ].join(';');
    const ctx = c.getContext('2d');
    ctx.fillStyle = '#1a1a2e';
    ctx.fillRect(0, 0, w, h);

    let _lastImg = null;
    const _repaint = () => {
        if (_lastImg) ctx.drawImage(_lastImg, 0, 0, w, h);
    };
    if (window.__canvasRepaintId) clearInterval(window.__canvasRepaintId);
    window.__canvasRepaintId = setInterval(_repaint, 66);
    window.__pushFrame = (b64) => {
        const img = new Image();
        img.onload = () => {
            _lastImg = img;
            ctx.drawImage(img, 0, 0, w, h);
        };
        img.src = 'data:image/jpeg;base64,' + b64;
    };

    // --- getDisplayMedia override (installed once) ---
    if (!window.__scOrigGDM) {
        const md = navigator.mediaDevices;
        window.__scOrigGDM = md.getDisplayMedia.bind(md);
        md.getDisplayMedia = async (constraints) => {
            constraints = constraints || {};
            constraints.audio = false;
            constraints.selfBrowserSurface = 'include';
            constraints.video = {displaySurface: 'browser'};
            try {
                const s = await window.__scOrigGDM(constraints);
                window.__scShareOk = true;
                return s;
            } catch (e) {
                window.__scShareOk = false;
                throw e;
            }
        };
    }
}"""

_REMOVE_OVERLAY_JS = """\
() => {
    const el = document.getElementById('__scOverlay');
    if (el) el.remove();
    window.__pushFrame = null;
    window.__scShareOk = null;
    if (window.__canvasRepaintId) {
        clearInterval(window.__canvasRepaintId);
        window.__canvasRepaintId = null;
    }
}"""

_SCREENCAST_QUALITY = 92


async def setup_content_stream(
    meeting_page: Page,
    content_page: Page,
    size: tuple[int, int] = (1280, 720),
) -> None:
    """通过 CDP 将 *content_page* 的画面流叠加到 *meeting_page*。

    在会议页安装画布叠加层并覆盖 ``getDisplayMedia``，随后在内容页启动 CDP
    截屏并将每一帧推送到叠加画布。

    参数:
        meeting_page: 会议标签页的 Playwright Page。
        content_page: 其帧将被共享的内容标签页 Page。
        size: 画布与截屏的宽高。
    """
    width, height = size
    await meeting_page.evaluate("() => { window.__scShareOk = null; }")
    await meeting_page.evaluate(_INSTALL_OVERLAY_JS, {"w": width, "h": height})

    cdp = await content_page.context.new_cdp_session(content_page)
    await cdp.send(
        "Page.startScreencast",
        {
            "format": "jpeg",
            "quality": _SCREENCAST_QUALITY,
            "maxWidth": width,
            "maxHeight": height,
            "everyNthFrame": 1,
        },
    )

    async def _on_frame(params: dict) -> None:  # type: ignore[type-arg]
        data = params.get("data", "")
        if data:
            await meeting_page.evaluate(
                "(b64) => window.__pushFrame?.(b64)",
                data,
            )
        await cdp.send(
            "Page.screencastFrameAck",
            {"sessionId": params.get("sessionId", 0)},
        )

    cdp.on("Page.screencastFrame", _on_frame)


async def remove_overlay(page: Page) -> None:
    """移除画布叠加层并重置所有注入的全局变量。"""
    await page.evaluate(_REMOVE_OVERLAY_JS)
