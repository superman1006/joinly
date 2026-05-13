FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

# 禁止 apt 弹出交互问答（tzdata 等）
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

# 换用阿里云 Ubuntu 镜像（AMD64）
RUN sed -i 's|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list \
 && sed -i 's|http://security.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list

# 安装 PulseAudio、Xvfb、VNC 服务 + Google Chrome（通过飞书浏览器检测）
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

# 安装 uv，强制使用系统 Python 3.12（避免 uv 自动下载 3.14）
RUN pip install --no-cache-dir uv
ENV UV_PYTHON=3.12

WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存
COPY pyproject.toml uv.lock README.md ./
COPY common/ common/
COPY client/ client/

# 复制主包源码
COPY joinly/ joinly/
COPY scripts/ scripts/

# 安装所有依赖（不含 dev）
RUN uv sync --frozen --no-dev

# 下载 ML 模型（Whisper、Kokoro、Silero VAD）
RUN uv run scripts/download_assets.py

ENTRYPOINT ["uv", "run", "joinly"]
