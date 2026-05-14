# ============================================================================
# Dockerfile for Joinly Feishu 飞书会议集成
# ============================================================================
# 构建说明：
#   docker build --platform linux/amd64 -t joinly-feishu:latest .
#
# 为什么用 --platform linux/amd64？
#   - 飞书需要 Google Chrome（仅有 x86_64 版本）
#   - Mac M1/M2 原生 arm64，不指定会构建 arm64 镜像无法装 Chrome
#   - 加此标志后 Mac 用 Rosetta 模拟 x86，构建出兼容飞书的 amd64 镜像
#
# 缓存策略：
#   - 只改业务代码：不重新下载 Python/依赖/模型，仅 3-5 秒
#   - 改 uv.lock：uv sync 重跑，Python/模型缓存复用，~30 秒
#   - 首次或清除缓存：全量下载 Python/依赖/模型，30-60 分钟
# ============================================================================

# 基础镜像：Playwright Python，包含 Chromium、Python 环境、常用工具
# 版本 v1.58.0，发行版 jammy（Ubuntu 22.04 LTS）
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

# ────────────────────────────────────────────────────────────────────────────
# 1. 系统环境配置
# ────────────────────────────────────────────────────────────────────────────

# 禁止 apt 弹出交互问答（如 tzdata 时区选择）
ENV DEBIAN_FRONTEND=noninteractive
# 设置时区为上海（CN），避免时间不一致问题
ENV TZ=Asia/Shanghai

# ────────────────────────────────────────────────────────────────────────────
# 2. 切换 apt 源到阿里云（加速下载）
# ────────────────────────────────────────────────────────────────────────────
# 原因：官方 Ubuntu 源在海外，Docker 构建时常超时
# 方案：sed 替换 archive.ubuntu.com → mirrors.aliyun.com，security 源同理

RUN sed -i 's|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list \
 && sed -i 's|http://security.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list

# ────────────────────────────────────────────────────────────────────────────
# 3. 安装系统依赖
# ────────────────────────────────────────────────────────────────────────────
# 分三部分：音视频虚拟化 + VNC 远程调试 + Google Chrome（飞书浏览器检测）
# ── 音视频虚拟化：模拟真实音视频设备 ──
#   pulseaudio：虚拟音频服务器，提供虚拟扬声器/麦克风
#   xvfb：虚拟 X11 显示服务器，无需物理显示器
# ── 远程调试：通过 VNC 客户端查看浏览器实时画面 ──
#   x11vnc：VNC 服务器，转发 Xvfb 画面到网络
# ── 辅助工具 ──
#   curl：下载工具（获取 Chrome 签名密钥）
#   gnupg：GPG 验证工具（验证 Chrome 软件包签名）
# ── Google Chrome：通过飞书的浏览器检测（必需）──
# 安装步骤：添加 Google 官方仓库 → 导入签名密钥 → 安装最新 stable 版本

RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio \
    xvfb \
    x11vnc \
    curl \
    gnupg \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor -o /etc/apt/trusted.gpg.d/google-chrome.gpg \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# ────────────────────────────────────────────────────────────────────────────
# 4. 安装 Python 包管理器 uv（替代 pip）
# ────────────────────────────────────────────────────────────────────────────
# uv 优势：更快、更可靠、支持 workspace、workspace 包自动链接
# 基础镜像自带的 Python 3.10 不满足项目要求 (>=3.12)，需让 uv 自动下载 3.12

RUN pip install --no-cache-dir uv

# ────────────────────────────────────────────────────────────────────────────
# 5. 配置 uv 的 Python 版本和 PyPI 镜像
# ────────────────────────────────────────────────────────────────────────────

# 指定 uv 使用 Python 3.12
# 原因：onnxruntime==1.21.1 仅有 cp312/cp313 wheel，不支持 3.14+
# 升级依赖时必须先确认 onnxruntime 新版本对目标 Python 的 wheel 支持
ENV UV_PYTHON=3.12

# 使用阿里云 PyPI 镜像加速依赖下载（Docker 构建环境无法直连 PyPI）
# 原因：构建层会重复下载 hatchling/editables 等，直连 PyPI 常超时
# 效果：避免 "operation timed out" 错误，加速构建 5-10 倍
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# ────────────────────────────────────────────────────────────────────────────
# 6. 设置工作目录
# ────────────────────────────────────────────────────────────────────────────
# 容器内的所有文件操作都在此目录下进行
WORKDIR /app

# ────────────────────────────────────────────────────────────────────────────
# 7. 复制依赖文件（第一步，利用 Docker 层缓存）
# ────────────────────────────────────────────────────────────────────────────
# 关键：先复制不常变的文件，后复制常变的文件
# 这样只改业务代码时，下面的 RUN uv sync 层不会失效，直接走缓存

# 复制 uv workspace 配置文件
COPY pyproject.toml uv.lock README.md ./
# 复制三个 workspace 子包的配置
COPY common/ common/
COPY client/ client/
# 复制构建脚本（ml 模型下载脚本）
COPY scripts/ scripts/

# ────────────────────────────────────────────────────────────────────────────
# 8. 安装 Python 依赖（会自动下载 Python 3.12）
# ────────────────────────────────────────────────────────────────────────────
# 缓存关键：此层在 "COPY joinly/" 之前，确保代码变化不会触发重新依赖安装
# --frozen：只使用 uv.lock 中锁定的版本，不升级任何包
# --no-dev：不安装开发依赖（pytest、ruff 等），减小镜像大小

RUN uv sync --frozen --no-dev

# ────────────────────────────────────────────────────────────────────────────
# 9. 下载 ML 模型（~2GB）
# ────────────────────────────────────────────────────────────────────────────
# 模型包括：
#   - Whisper (base)：语音转文本，~140MB
#   - Kokoro v1.0：文本转语音，~900MB
#   - Silero VAD：语音活动检测，~40MB
# 缓存机制：此层依赖 uv sync，只改代码时走缓存
# 首次下载需要网络访问模型仓库（Hugging Face 等），可能需要 5-10 分钟

RUN uv run scripts/download_assets.py

# ────────────────────────────────────────────────────────────────────────────
# 10. 复制主包源码（最后一步，频繁变动）
# ────────────────────────────────────────────────────────────────────────────
# 策略：将常变的代码放在最后，不影响前面的缓存层
# 好处：只修改 joinly/ 代码时，前面的 Python/模型 层全部走缓存，重建只需 3-5 秒

COPY joinly/ joinly/

# ────────────────────────────────────────────────────────────────────────────
# 11. 复制资源文件（Logo、图标等）
# ────────────────────────────────────────────────────────────────────────────
# transsionLOGO.png：虚拟摄像头中显示的 Logo
# 这些文件打包进镜像，运行时无需外部挂载，就像代码一样

COPY *.png ./

# ────────────────────────────────────────────────────────────────────────────
# 12. 运行时环境变量（放在最后，不影响上面任何构建层的缓存）
# ────────────────────────────────────────────────────────────────────────────
# Feishu Cookie 文件在容器内的默认路径（与 docker run -v 挂载路径保持一致）
# 运行时只需 -v $(pwd)/feishu_cookies.json:/cookies/feishu_cookies.json:ro，无需再传 -e
ENV JOINLY_FEISHU_COOKIES_FILE=/cookies/feishu_cookies.json

# ────────────────────────────────────────────────────────────────────────────
# 13. 设置容器启动命令
# ────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT：容器启动时执行的命令，所有参数追加到后面
# 例：docker run joinly-feishu --client <URL>
#     → 容器执行：uv run joinly --client <URL>

ENTRYPOINT ["uv", "run", "joinly"]
