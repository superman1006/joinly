FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

# 禁止 apt 弹出交互问答（tzdata 等）
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Shanghai

# 换用清华大学 Ubuntu Ports 镜像（ARM64 加速）
RUN sed -i 's|http://ports.ubuntu.com/ubuntu-ports|http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports|g' \
    /etc/apt/sources.list

# 安装 PulseAudio、Xvfb、VNC 服务
RUN apt-get update && apt-get install -y --no-install-recommends \
    pulseaudio \
    xvfb \
    x11vnc \
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
