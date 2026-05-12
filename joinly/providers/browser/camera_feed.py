"""通过覆盖 getUserMedia 与 RTCPeerConnection 实现的虚拟摄像头画面。

覆盖 ``navigator.mediaDevices.getUserMedia``，使视频请求返回基于画布的
``MediaStreamTrack`` 而非真实摄像头，音频请求仍走真实设备。

同时修补 ``RTCPeerConnection.prototype.addTrack``，将任意视频轨替换为
画布轨，确保 WebRTC 协商始终使用虚拟画面而不受各平台差异影响。

修补 ``enumerateDevices`` 以包含虚拟摄像头，使依赖摄像头枚举的平台
仍能显示视频开关。

画布直接渲染 Joinly 标识（不经 CDP 投屏、不经 JPEG 压缩）。
音频幅度驱动实时均衡器式动效。
"""

import asyncio
from collections.abc import Callable

import numpy as np
from playwright.async_api import Page

from joinly.core import AudioWriter

_CAM_WIDTH = 1280
_CAM_HEIGHT = 720
_BAND_THROTTLE_S = 0.05
_NUM_BANDS = 7

# Logo SVG 的 data URI，在画布上作为 Image 加载
_LOGO_SVG = (
    "data:image/svg+xml,"
    "%3Csvg viewBox='0 0 509 508' xmlns='http://www.w3.org/2000/svg'"
    " style='fill-rule:evenodd;clip-rule:evenodd;"
    "stroke-linejoin:round;stroke-miterlimit:2'%3E"
    "%3Cg transform='matrix(0.198828,0,0,1,0,0)'%3E"
    "%3Crect x='0' y='0' width='2560' height='507.274'"
    " style='fill:none'/%3E"
    "%3Cg%3E%3Cg transform="
    "'matrix(18.9194,0,0,3.74809,-1607.95,-6354.86)'%3E"
    "%3Cg transform='matrix(6.03591e-17,-0.985739,0.986051,"
    "6.03782e-17,-102.185,1960.59)'%3E"
    "%3Cpath d='M268.936,224.012C268.936,205.142 253.555,189.822 "
    "234.611,189.822L165.961,189.822C147.016,189.822 131.636,"
    "205.142 131.636,224.012L131.636,292.846C131.636,311.716 "
    "147.016,327.036 165.961,327.036L234.611,327.036C253.555,"
    "327.036 268.936,311.716 268.936,292.846L268.936,224.012Z'/%3E"
    "%3C/g%3E%3Cg%3E%3Cg transform='matrix(-1.66394e-16,0.905807,"
    "-0.905807,-1.66394e-16,618.204,708.95)'%3E"
    "%3Cpath d='M1147.84,552.057C1159.51,552.057 1168.44,547.024 "
    "1173.91,539.258L1173.91,544.701C1173.91,546.155 1174.49,"
    "547.55 1175.52,548.579C1176.55,549.607 1177.94,550.185 "
    "1179.4,550.185C1183.66,550.185 1188.87,550.185 1188.87,"
    "550.185L1188.87,477.771L1179.46,477.771C1177.99,477.771 "
    "1176.58,478.355 1175.54,479.395C1174.5,480.436 1173.91,"
    "481.847 1173.91,483.318L1173.91,488.698C1168.44,480.932 "
    "1159.51,475.899 1147.84,475.899C1127.38,475.899 1111.85,"
    "492.154 1111.85,513.906C1111.85,535.802 1127.38,552.057 "
    "1147.84,552.057ZM1150.29,538.539C1136.46,538.539 1126.66,"
    "528.167 1126.66,513.906C1126.66,499.645 1136.46,489.417 "
    "1150.29,489.417C1163.54,489.417 1174.2,499.789 1174.2,"
    "513.906C1174.2,528.167 1163.54,538.539 1150.29,538.539Z'"
    " style='fill:white;fill-rule:nonzero'/%3E%3C/g%3E"
    "%3Cg transform='matrix(1.6197e-16,0.905807,0.712479,"
    "-1.35305e-16,-204.864,701.281)'%3E"
    "%3Crect x='1209.34' y='477.771' width='14.958' height='72.414'"
    " style='fill:white;fill-rule:nonzero'/%3E%3C/g%3E"
    "%3Cg transform='matrix(-0.901226,0,0,0.901226,"
    "439.829,1382.51)'%3E"
    "%3Ccircle cx='349.421' cy='467.11' r='7.517'"
    " style='fill:white'/%3E%3C/g%3E%3C/g%3E%3C/g%3E"
    "%3C/g%3E%3C/g%3E%3C/svg%3E"
)

# ---------------------------------------------------------------------------
# 状态动效函数：各函数在标识下方绘制一小段动画。
# 为可读性拆成独立 JS 函数体，经 _CAMERA_OVERRIDE_TEMPLATE 插入主渲染循环。
# ---------------------------------------------------------------------------

# 发言：由真实 FFT 频段驱动的频谱柱
_FX_SPEAKING = """\
function fxSpeaking(ctx, cx, y, bands, alpha) {
    const N = bands.length;
    const gap = H * 0.012, barW = H * 0.006;
    const ox = cx - (N - 1) * gap / 2;
    ctx.lineCap = 'round';
    for (let i = 0; i < N; i++) {
        const v = Math.min(bands[i] * 6, 1);
        if (v < 0.01) continue;
        const h = H * 0.004 + H * 0.028 * v;
        ctx.globalAlpha = (0.25 + v * 0.4) * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.roundRect(ox + i * gap - barW / 2, y - h,
            barW, h * 2, barW / 2);
        ctx.fill();
    }
}"""

# 输入中：三点顺序弹跳
_FX_TYPING = """\
function fxTyping(ctx, cx, y, t, alpha) {
    const N = 3, gap = H * 0.024, r = H * 0.008;
    const ox = cx - (N - 1) * gap / 2;
    for (let i = 0; i < N; i++) {
        const phase = (t * 4 - i * 0.9) % (Math.PI * 2);
        const raw = Math.sin(phase);
        const bounce = raw > 0 ? Math.pow(raw, 0.8) : 0;
        const dy = bounce * H * 0.018;
        ctx.globalAlpha = (0.3 + bounce * 0.5) * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(ox + i * gap, y - dy, r, 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# 共享屏幕：自标识尺寸向外扩张的圆角矩形（绘在标识背后）
_FX_SHARE = """\
function fxShare(ctx, cx, cy, logoW, logoH, t, alpha) {
    const endW = logoW * 2, endH = logoH * 1.8;
    for (let i = 0; i < 3; i++) {
        const p = ((t * 0.35 + i / 3) % 1);
        const ease = 1 - Math.pow(1 - p, 2.5);
        const w = logoW * 0.5 + (endW - logoW * 0.5) * ease;
        const h = logoH * 0.5 + (endH - logoH * 0.5) * ease;
        const fade = (1 - p) * alpha * 0.5;
        if (fade < 0.01) continue;
        ctx.globalAlpha = fade;
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2 - ease;
        ctx.beginPath();
        ctx.roundRect(cx - w / 2, cy - h / 2, w, h,
            6 + ease * 4);
        ctx.stroke();
    }
}"""

# 被打断：点从中心散开并淡出
_FX_INTERRUPTED = """\
function fxInterrupted(ctx, cx, y, t, alpha) {
    const N = 5, r = H * 0.007;
    for (let i = 0; i < N; i++) {
        const angle = (i / N) * Math.PI * 2 + t * 1.5;
        const p = (t * 2.5 + i / N) % 1;
        const spread = H * 0.01 + p * H * 0.04;
        const dx = Math.cos(angle) * spread;
        const dy = Math.sin(angle) * spread * 0.5;
        const fade = (1 - p) * alpha;
        if (fade < 0.01) continue;
        ctx.globalAlpha = fade;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(cx + dx, y + dy, r * (1 - p * 0.5), 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# 思考中：标识周围旋转的弧段与柔和光晕
_FX_THINKING = """\
function fxThinking(ctx, cx, cy, logoW, logoH, t, alpha) {
    const r = Math.max(logoW, logoH) * 0.62;
    const pulse = 0.5 + 0.5 * Math.sin(t * 2.0);

    // Outer glow ring — subtle breathing
    ctx.globalAlpha = (0.06 + pulse * 0.06) * alpha;
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = H * 0.012;
    ctx.beginPath();
    ctx.arc(cx, cy, r + H * 0.006, 0, Math.PI * 2);
    ctx.stroke();

    // Rotating arc segments — 3 arcs at different speeds
    for (let i = 0; i < 3; i++) {
        const speed = 1.2 + i * 0.4;
        const dir = i % 2 ? -1 : 1;
        const base = t * speed * dir + i * Math.PI * 0.667;
        const len = Math.PI * (0.3 + 0.15 * Math.sin(t * 1.5 + i));
        ctx.globalAlpha = (0.2 + (1 - i * 0.25) * 0.25) * alpha;
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2 - i * 0.4;
        ctx.beginPath();
        ctx.arc(cx, cy, r + H * (0.002 + i * 0.006),
            base, base + len);
        ctx.stroke();
    }

    // Orbiting dots — 2 dots at different orbits
    for (let i = 0; i < 2; i++) {
        const a = t * (1.6 + i * 0.5) + i * Math.PI;
        const orbitR = r + H * (0.01 + i * 0.008);
        const dx = Math.cos(a) * orbitR;
        const dy = Math.sin(a) * orbitR;
        const dotPulse = 0.5 + 0.5 * Math.sin(t * 3 + i * 2);
        ctx.globalAlpha = (0.35 + dotPulse * 0.4) * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(cx + dx, cy + dy,
            H * (0.005 + dotPulse * 0.002), 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# 忙碌：雷达扫描与拖尾粒子
_FX_BUSY = """\
function fxBusy(ctx, cx, y, t, alpha) {
    const w = H * 0.08;
    const speed = 0.6;
    const p = (t * speed) % 2;
    const dir = p <= 1 ? 1 : -1;
    const norm = p <= 1 ? p : p - 1;
    const ease = norm < 0.5
        ? 2 * norm * norm
        : 1 - 2 * (1 - norm) * (1 - norm);
    const x = dir > 0
        ? cx - w + ease * w * 2
        : cx + w - ease * w * 2;

    // Glow line
    const grad = ctx.createLinearGradient(
        x - H * 0.015, y, x + H * 0.015, y);
    grad.addColorStop(0, 'rgba(255,255,255,0)');
    grad.addColorStop(0.5, 'rgba(255,255,255,1)');
    grad.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.globalAlpha = 0.5 * alpha;
    ctx.strokeStyle = grad;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, y - H * 0.018);
    ctx.lineTo(x, y + H * 0.018);
    ctx.stroke();

    // Centre dot
    ctx.globalAlpha = 0.6 * alpha;
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(x, y, H * 0.004, 0, Math.PI * 2);
    ctx.fill();

    // Trail particles
    for (let i = 1; i <= 5; i++) {
        const d = i * 0.04;
        const tn = p <= 1 ? Math.max(0, p - d) : Math.max(0, (p - 1) - d);
        const te = tn < 0.5
            ? 2 * tn * tn
            : 1 - 2 * (1 - tn) * (1 - tn);
        const tx = dir > 0
            ? cx - w + te * w * 2
            : cx + w - te * w * 2;
        const fade = (1 - i / 6);
        ctx.globalAlpha = fade * 0.35 * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(tx, y, H * (0.004 - i * 0.0004), 0, Math.PI * 2);
        ctx.fill();
    }

    // Static endpoint markers
    ctx.globalAlpha = 0.12 * alpha;
    ctx.fillStyle = '#ffffff';
    for (const ex of [cx - w, cx + w]) {
        ctx.beginPath();
        ctx.arc(ex, y, H * 0.003, 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# 阅读中：带拖尾的点左右扫动
_FX_READING = """\
function fxReading(ctx, cx, y, t, alpha) {
    const w = H * 0.035;
    const r = H * 0.008;
    const p = (t * 1.8 % 2);
    for (let i = 0; i < 3; i++) {
        const d = i * 0.1;
        const tp = p <= 1
            ? cx - w + Math.max(0, p - d) * w * 2
            : cx + w - Math.max(0, (p - 1) - d) * w * 2;
        ctx.globalAlpha = (0.15 + (1 - i / 3) * 0.35) * alpha;
        ctx.fillStyle = '#ffffff';
        ctx.beginPath();
        ctx.arc(tp, y, r * (1 - i * 0.12), 0, Math.PI * 2);
        ctx.fill();
    }
}"""

# 初始化脚本仅打补丁 API（不访问 DOM）。
# 画布/Image/rAF 相关工作推迟到首次 getUserMedia 时由 _initCanvas 执行
# （此时 DOM 已就绪）。
_CAMERA_OVERRIDE_TEMPLATE = """\
(() => {{
    const W = {w}, H = {h};
    const LOGO_SRC = "{logo_svg}";

    if (window.__camOrigGUM) return;

    let camTrack = null;

    {fx_speaking}
    {fx_typing}
    {fx_share}
    {fx_reading}
    {fx_interrupted}
    {fx_thinking}
    {fx_busy}

    const FX = {{
        typing: fxTyping,
        reading: fxReading,
        interrupted: fxInterrupted,
        busy: fxBusy,
    }};

    const FX_BG = {{
        thinking: fxThinking,
        sharing: fxShare,
    }};

    function _initCanvas() {{
        if (camTrack) return camTrack;

        const c = document.createElement('canvas');
        c.width = W; c.height = H;
        const ctx = c.getContext('2d');

        let logoImg = null;
        const bands = new Float32Array({n_bands});
        const smoothBands = new Float32Array({n_bands});
        let t = 0;
        let status = '';
        let statusAlpha = 0;
        let statusT = 0;
        let statusSetAt = 0;
        const STATUS_MIN_MS = 1500;

        const img = new Image();
        img.onload = () => {{ logoImg = img; }};
        img.src = LOGO_SRC;

        window.__setBands = (b) => {{
            for (let i = 0; i < bands.length; i++)
                bands[i] = b[i] || 0;
        }};
        window.__setStatus = (s) => {{
            if (s) {{
                status = s;
                statusT = 0;
                statusSetAt = performance.now();
            }} else {{
                const elapsed = performance.now() - statusSetAt;
                if (elapsed >= STATUS_MIN_MS) {{
                    status = '';
                }} else {{
                    setTimeout(() => {{ status = ''; }},
                        STATUS_MIN_MS - elapsed);
                }}
            }}
        }};

        function draw() {{
            t += 0.02;

            ctx.fillStyle = '#121220';
            ctx.fillRect(0, 0, W, H);

            if (logoImg) {{
                const logoH = H * 0.35;
                const logoW = logoH;
                const cx = W / 2;
                const cy = H / 2;
                const logoBot = cy + logoH / 2;

                // Action status (compute alpha for all effects)
                const wantAlpha = status ? 1 : 0;
                statusAlpha += (wantAlpha - statusAlpha) * 0.12;
                if (status) statusT += 0.02;

                // Background effects — behind the logo
                if (statusAlpha > 0.02) {{
                    const bgFn = FX_BG[status];
                    if (bgFn) {{
                        ctx.save();
                        bgFn(ctx, cx, cy, logoW, logoH,
                            statusT, statusAlpha);
                        ctx.restore();
                    }}
                }}

                // Speaking — behind the logo
                let anyBand = false;
                for (let i = 0; i < bands.length; i++) {{
                    smoothBands[i] += (bands[i] - smoothBands[i]) * 0.3;
                    if (smoothBands[i] < 0.005) smoothBands[i] = 0;
                    if (smoothBands[i] > 0) anyBand = true;
                    bands[i] *= 0.75;
                }}
                if (anyBand) {{
                    ctx.save();
                    fxSpeaking(ctx, cx, logoBot + H * 0.04,
                        smoothBands, 1);
                    ctx.restore();
                }}

                ctx.drawImage(
                    logoImg,
                    cx - logoW / 2, cy - logoH / 2,
                    logoW, logoH
                );

                // Foreground effects — below the logo
                if (statusAlpha > 0.02) {{
                    const fn = FX[status];
                    if (fn) {{
                        ctx.save();
                        fn(ctx, cx, logoBot + H * 0.08,
                            statusT, statusAlpha);
                        ctx.restore();
                    }}
                }}
            }}
            requestAnimationFrame(draw);
        }}
        requestAnimationFrame(draw);

        camTrack = c.captureStream(30).getVideoTracks()[0];
        return camTrack;
    }}

    const md = navigator.mediaDevices;

    window.__camOrigGUM = md.getUserMedia.bind(md);
    md.getUserMedia = async (constraints) => {{
        const wantsVideo = !!constraints?.video;
        const wantsAudio = !!constraints?.audio;

        if (wantsAudio) {{
            const real = await window.__camOrigGUM({{
                audio: constraints.audio,
                video: false,
            }});
            if (wantsVideo) real.addTrack(_initCanvas().clone());
            return real;
        }}
        if (wantsVideo) {{
            return new MediaStream([_initCanvas().clone()]);
        }}
        return window.__camOrigGUM(constraints);
    }};

    const origAddTrack = RTCPeerConnection.prototype.addTrack;
    RTCPeerConnection.prototype.addTrack = function(track, ...streams) {{
        if (track.kind === 'video') {{
            return origAddTrack.call(
                this, _initCanvas().clone(), ...streams
            );
        }}
        return origAddTrack.call(this, track, ...streams);
    }};

    const origEnum = md.enumerateDevices.bind(md);
    md.enumerateDevices = async () => {{
        const devices = await origEnum();
        const hasCamera = devices.some(d => d.kind === 'videoinput');
        if (!hasCamera) {{
            devices.push({{
                deviceId: 'virtual-camera',
                groupId: 'virtual',
                kind: 'videoinput',
                label: 'Virtual Camera',
                toJSON() {{ return this; }},
            }});
        }}
        return devices;
    }};
}})();"""


class CameraFeed:
    """管理虚拟摄像头画布与由幅度驱动的光晕效果。

    Draws the Joinly logo directly on the camera canvas (no CDP
    screencast).  Wraps an ``AudioWriter`` to extract amplitude and
    push it to the canvas render loop.
    """

    def __init__(self, writer: AudioWriter) -> None:
        """使用底层音频写入端初始化。"""
        self._meeting_page: Page | None = None
        self._last_band_time: float = 0
        self.audio_writer = _AmplitudeAudioWriter(writer, self._on_bands)

    async def install(self, meeting_page: Page) -> None:
        """在会议页面安装 getUserMedia 覆盖逻辑。"""
        self._meeting_page = meeting_page
        script = _CAMERA_OVERRIDE_TEMPLATE.format(
            w=_CAM_WIDTH,
            h=_CAM_HEIGHT,
            n_bands=_NUM_BANDS,
            logo_svg=_LOGO_SVG,
            fx_speaking=_FX_SPEAKING,
            fx_typing=_FX_TYPING,
            fx_share=_FX_SHARE,
            fx_reading=_FX_READING,
            fx_interrupted=_FX_INTERRUPTED,
            fx_thinking=_FX_THINKING,
            fx_busy=_FX_BUSY,
        )
        await meeting_page.add_init_script(script)

    def set_effect(self, name: str | None) -> None:
        """设置当前视觉效果；传入 None 清除。"""
        page = self._meeting_page
        if page and not page.is_closed():
            safe = (name or "").replace("'", "\\'")
            task = asyncio.ensure_future(
                page.evaluate(f"window.__setStatus?.('{safe}')")
            )
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )

    async def stop(self) -> None:
        """清理引用。"""
        self._meeting_page = None

    def _on_bands(self, bands: list[float]) -> None:
        now = asyncio.get_event_loop().time()
        if now - self._last_band_time < _BAND_THROTTLE_S:
            return
        self._last_band_time = now
        page = self._meeting_page
        if page and not page.is_closed():
            arr = "[" + ",".join(f"{v:.4f}" for v in bands) + "]"
            task = asyncio.ensure_future(page.evaluate(f"window.__setBands?.({arr})"))
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )


class _AmplitudeAudioWriter(AudioWriter):
    """按音频块计算频段的音频写入端。"""

    def __init__(
        self,
        writer: AudioWriter,
        on_bands: Callable[[list[float]], None],
    ) -> None:
        self._writer = writer
        self._on_bands = on_bands
        self.audio_format = writer.audio_format
        self.chunk_size = writer.chunk_size

    async def write(self, data: bytes) -> None:
        """写入音频并转发各频段能量。"""
        n_samples = len(data) // 2
        if n_samples < _NUM_BANDS:
            await self._writer.write(data)
            return
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        fft = np.abs(np.fft.rfft(samples))
        # 归一化：FFT 幅度随采样点数与量化范围缩放
        fft /= n_samples * 32768
        # 对数间隔的频段边界，使低频有更高分辨率
        n_bins = len(fft)
        edges = np.logspace(np.log10(1), np.log10(n_bins), _NUM_BANDS + 1).astype(int)
        edges = np.clip(edges, 0, n_bins)
        bands = [
            float(np.mean(fft[edges[i] : max(edges[i + 1], edges[i] + 1)]))
            for i in range(_NUM_BANDS)
        ]
        self._on_bands(bands)
        await self._writer.write(data)
