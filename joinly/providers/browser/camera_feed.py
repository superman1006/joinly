"""通过覆盖 getUserMedia 与 RTCPeerConnection 实现的虚拟摄像头画面。

覆盖 ``navigator.mediaDevices.getUserMedia``，使视频请求返回基于画布的
``MediaStreamTrack`` 而非真实摄像头，音频请求仍走真实设备。

同时修补 ``RTCPeerConnection.prototype.addTrack``，将任意视频轨替换为
画布轨，确保 WebRTC 协商始终使用虚拟画面而不受各平台差异影响。

修补 ``enumerateDevices`` 以包含虚拟摄像头，使依赖摄像头枚举的平台
仍能显示视频开关。

画布直接渲染会议标识图像（transsionLOGO.png，不经 CDP 投屏、不经 JPEG 压缩）。
音频幅度驱动实时均衡器式动效。
"""

import asyncio
import base64
from collections.abc import Callable
from pathlib import Path

import numpy as np
from playwright.async_api import Page

from joinly.core import AudioWriter

_CAM_WIDTH = 1280
_CAM_HEIGHT = 720
_BAND_THROTTLE_S = 0.05
_NUM_BANDS = 7


def _transsion_logo_data_uri() -> str:
    """从仓库根目录的 transsionLOGO.png 生成可在画布 Image 中使用的 data URI。"""
    repo_root = Path(__file__).resolve().parents[3]
    logo_path = repo_root / "transsionLOGO.png"
    if not logo_path.is_file():
        msg = (
            f"未找到虚拟摄像头用标识图：{logo_path}。"
            "请将 transsionLOGO.png 放在项目根目录。"
        )
        raise FileNotFoundError(msg)
    raw = logo_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


_LOGO_DATA_URI = _transsion_logo_data_uri()
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
    const LOGO_SRC = "{logo_src}";

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

    在画布上绘制标识图像（transsionLOGO.png）。``AudioWriter`` 包装用于提取幅度并
    推送到画布渲染循环。
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
            logo_src=_LOGO_DATA_URI,
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
