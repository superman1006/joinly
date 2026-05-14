<p align="center">
  <img alt="Transsion" src="./transsionLOGO.png" width="420">
</p>

<h1 align="center">让 AI 智能体加入你的会议 🤖</h1>

**joinly** 是一个连接中间件，使 AI 智能体能够加入并主动参与视频会议。通过 MCP 服务器，joinly 提供了必要的[会议工具](#工具)和[资源](#资源)，使任何 AI 智能体都能在实时会议中执行任务并与你交互。

> 想立即开始？跳到[快速开始](#快速开始)！

> [!IMPORTANT]  
> 不想折腾？试试我们的[云版本](https://cloud.joinly.ai)！☁️🚀

# ✨ 功能特性

- **实时交互**：智能体通过语音或聊天在会议中实时执行任务并回复
- **自然对话流**：内置逻辑处理中断和多人交互
- **跨平台支持**：支持 Google Meet、飞书、Zoom、Teams 等（或任何浏览器可访问的会议）
- **自带你的 LLM**：支持所有 LLM 提供商（也支持本地 Ollama）
- **灵活的 TTS/STT**：支持多种服务 - Whisper/Deepgram 用于 STT，Kokoro/ElevenLabs/Deepgram 用于 TTS
- **100% 开源、自托管、隐私优先** :rocket:

# ⚡ 快速开始

通过 Docker 运行 joinly，配合对话 AI 客户端。

## 系统要求

> [!IMPORTANT]
> **必须安装**：
> - [Docker Desktop](https://docs.docker.com/engine/install/) （包含 Docker 引擎）
> - **至少 50GB 磁盘空间**（镜像 ~2.3GB + ML 模型 ~2GB + 工作目录）
> - **网络**：能访问 GitHub、PyPI、模型仓库（如需翻墙，请提前开启 VPN）

### 针对飞书用户的额外要求

如果你想使用**飞书**（vc.feishu.cn）：
- **CPU 架构**：需要构建 AMD64 镜像（包含 Google Chrome）
- **Python 版本**：镜像内固定使用 **Python 3.12**（由 `ENV UV_PYTHON=3.12` 控制）。`onnxruntime==1.21.1` 仅提供 cp312/cp313 wheel，**不支持 Python 3.13+**，请勿随意升级
- **构建环境**：
  - Docker 必须支持多平台构建（`docker build --platform linux/amd64`）
  - 在 Apple Silicon Mac 上，Docker Desktop 会自动用 Rosetta 模拟 AMD64
  - **构建时间**：首次构建 30-60 分钟（下载 Ubuntu、Chrome、Python 3.12、ML 模型）
  - **网络稳定**：Chrome 下载来自 Google，建议全程开启 VPN

## 步骤 1：创建 .env 文件

在项目根目录创建 `.env` 文件，填入你的 LLM 配置（以 OpenAI 为例）：

```dotenv
# .env 示例
JOINLY_LLM_MODEL=gpt-4o
JOINLY_LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-api-key
JOINLY_NAME=Joinly AI
```

> [!TIP]
> - OpenAI API 密钥：https://platform.openai.com/api-keys
> - 完整配置选项见 [.env.example](.env.example)（包括 Claude、Ollama 等）

## 步骤 2：启动容器

**Google Meet 或 Zoom（使用官方镜像，推荐新手）：**

```bash
docker run --env-file .env \
  ghcr.io/joinly-ai/joinly:latest \
  --client "<MeetingURL>"
```

**飞书（需要本地构建镜像）：**

首先构建镜像（**第一次需要 30-60 分钟**，请确保 VPN 已开启）：

```bash
docker build --platform linux/amd64 -t joinly-feishu .
```

**后续只改代码时**（不改依赖），用以下命令快速重建，Docker 会复用缓存、不重新下载任何东西：

```bash
docker build --platform linux/amd64 --cache-from joinly-feishu:latest -t joinly-feishu:latest .
```

然后运行（见下方[飞书支持](#飞书-飞书-支持)章节）。

> :red_circle: 遇到问题？加我们的 [Discord](https://discord.com/invite/AN5NEBkS4d)!

# 🐦 飞书（飞书）支持

本项目添加了对**飞书** 视频会议（`vc.feishu.cn`）的支持。

> [!IMPORTANT]
> 飞书需要预认证的浏览器会话才能通过网页加入会议。你需要导出一次飞书登录 Cookie 并提供给容器。

## 前置条件

本仓库需要从源码构建镜像（官方 `joinly-ai/joinly` 镜像不支持飞书）：

```bash
docker build --platform linux/amd64 -t joinly-feishu .
```

> **说明**：`--platform linux/amd64` 标志是必需的，以安装 Google Chrome（用于通过飞书的浏览器检测）。在 Apple Silicon Mac 上，Docker 会通过 Rosetta 模拟 AMD64 运行。

**后续只改代码时**（不改依赖），用以下命令快速重建，Docker 会复用缓存、不重新下载任何东西：

```bash
docker build --platform linux/amd64 --cache-from joinly-feishu:latest -t joinly-feishu:latest .
```

## 步骤 1：导出飞书 Cookie

**第一步：安装 Cookie-Editor 扩展**

在 Google Chrome 中打开以下链接，安装扩展：

```
https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm
```

**第二步：登录飞书网页版**

在 Chrome 中打开：

```
https://vc.feishu.cn
```

用手机号 + 短信验证码登录飞书账号，登录成功后页面会显示"发起新会议 / 加入会议"。

**第三步：导出 Cookie**

1. 保持在 `vc.feishu.cn` 页面，点击浏览器右上角的 **Cookie-Editor 扩展图标**（如果没有，点击拼图图标在扩展列表中找到它）
2. 扩展面板打开后，点击底部的 **Export** 按钮
3. 选择 **Export as JSON**
4. 此时 JSON 内容已复制到剪贴板

**第四步：保存文件**

在项目根目录新建文件 `feishu_cookies.json`，将剪贴板内容粘贴进去并保存：

```
joinly/
├── feishu_cookies.json   ← 新建这个文件，粘贴 Cookie 内容
├── .env
├── Dockerfile
└── ...
```

> [!WARNING]
> `feishu_cookies.json` 包含你的飞书登录令牌。**绝不要提交到公开仓库**。该文件已列入 `.gitignore`。

## 步骤 2：运行（带 Cookie）

> [!IMPORTANT]
> 必须在**项目根目录**下执行以下命令，否则 `.env` 和 `feishu_cookies.json` 路径找不到。

```bash
cd /path/to/joinly   # 先进入项目根目录
```

然后启动容器：

```bash
docker run -d \
  --name joinly-feishu \
  --env-file .env \
  -e JOINLY_FEISHU_COOKIES_FILE=/cookies/feishu_cookies.json \
  -v $(pwd)/feishu_cookies.json:/cookies/feishu_cookies.json:ro \
  joinly-feishu \
  --client "https://vc.feishu.cn/j/<会议ID>"
```

参数说明：
- `-d`：后台运行，终端不会被阻塞
- `--name joinly-feishu`：指定容器名称，便于后续管理（不加则随机命名）
- `--env-file .env`：加载配置文件（必须在项目目录下执行）
- `-v $(pwd)/feishu_cookies.json:...`：将本地 Cookie 文件挂载进容器

**查看运行日志：**

```bash
docker logs -f joinly-feishu
```

**停止容器：**

```bash
docker stop joinly-feishu && docker rm joinly-feishu
```

Bot 加入会议后会自动：

1. 访问会议链接
2. 点击**网页版入会**
3. 填入显示名称（来自 `.env` 的 `JOINLY_NAME`）
4. 点击**加入**进入会议

## Cookie 过期

飞书登录会话会在一段时间不活动后过期。如果 Bot 无法加入（卡在登录页），请重新导出 Cookie 并替换 `feishu_cookies.json`。

---

# 👨‍💻 运行外部客户端

在快速开始中，我们直接以 `--client` 模式运行容器。你也可以把它作为服务器运行，从容器外部连接，这样就能接入其他 MCP 服务器。此处使用 [joinly-client 包](https://pypi.org/project/joinly-client/)运行客户端。

> [!IMPORTANT]
> **前置条件**：完成[快速开始](#快速开始)的准备步骤、[安装 uv](https://github.com/astral-sh/uv)、打开两个终端

在第一个终端启动 joinly 服务器（注意：不使用 `--client`，转发端口 `8000`）：

```bash
docker run -p 8000:8000 ghcr.io/joinly-ai/joinly:latest
```

在第二个终端运行客户端连接到服务器并加入会议：

```bash
uvx joinly-client --env-file .env <MeetingUrl>
```

## 为客户端添加 MCP 服务器

通过 JSON 配置为客户端添加任何 MCP 服务器的工具。配置文件可在 `"mcpServers"` 下包含多个条目，这些工具都将在会议中可用（见 [fastmcp 客户端文档](https://gofastmcp.com/clients/client)）：

```json
{
  "mcpServers": {
    "localServer": {
      "command": "npx",
      "args": ["-y", "package@0.1.0"]
    },
    "remoteServer": {
      "url": "http://mcp.example.com",
      "auth": "oauth"
    }
  }
}
```

例如添加 [Tavily 配置](examples/config_tavily.json)以进行网络搜索，然后使用配置文件运行客户端：

```bash
uvx joinly-client --env-file .env --mcp-config config.json <MeetingUrl>
```

# ⚙️ 配置选项

配置可通过环境变量和/或命令行参数指定。以下是启动 Docker 容器时常用的配置选项：

```bash
docker run --env-file .env -p 8000:8000 ghcr.io/joinly-ai/joinly:latest <MyOptionArgs>
```

或者，你可以在 `joinly-client` 中通过命令行参数传递 `--name`、`--lang` 和[提供商设置](#提供商)，这些会覆盖服务器设置：

```bash
uvx joinly-client <MyOptionArgs> <MeetingUrl>
```

## 基本设置

Docker 镜像默认启动 MCP 服务器。为快速上手，我们也提供了可通过 `--client` 使用的客户端实现。此时不启动服务器，其他客户端无法连接。

```bash
# 直接作为客户端启动；默认是服务器，外部客户端可连接
--client <MeetingUrl>

# 改变参与者名称（默认：joinly）
--name "AI Assistant"

# 改变 TTS/STT 语言（默认：en）
# 注：可用性取决于 TTS/STT 提供商
--lang zh-CN

# 改变 joinly MCP 服务器的主机和端口
--host 0.0.0.0 --port 8000
```

## Providers

### Text-to-Speech

```bash
# Kokoro (local) TTS (default)
--tts kokoro
--tts-arg voice=<VoiceName>  # optionally, set different voice

# ElevenLabs TTS, include ELEVENLABS_API_KEY in .env
--tts elevenlabs
--tts-arg voice_id=<VoiceID>  # optionally, set different voice

# Deepgram TTS, include DEEPGRAM_API_KEY in .env
--tts deepgram
--tts-arg model_name=<ModelName>  # optionally, set different model (voice)
```

### Transcription

```bash
# Whisper (local) STT (default)
--stt whisper
--stt-arg model_name=<ModelName>  # optionally, set different model (default: base), for GPU support see below

# Deepgram STT, include DEEPGRAM_API_KEY in .env
--stt deepgram
--stt-arg model_name=<ModelName>  # optionally, set different model
```

## Debugging

```bash
# Start browser with a VNC server for debugging;
# forward the port and connect to it using a VNC client
--vnc-server --vnc-server-port 5900

# Logging
-v  # or -vv, -vvv

# Help
--help
```

## GPU Support

We provide a Docker image with CUDA GPU support for running the transcription and TTS models on a GPU. To use it, you need to have the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) installed and `CUDA >= 12.6`. Then pull the CUDA-enabled image:

```bash
docker pull ghcr.io/joinly-ai/joinly:latest-cuda
```

Run as client or server with the same commands as above, but use the `joinly:{version}-cuda` image and set `--gpus all`:

```bash
# Run as server
docker run --gpus all --env-file .env -p 8000:8000 ghcr.io/joinly-ai/joinly:latest-cuda -v
# Run as client
docker run --gpus all --env-file .env ghcr.io/joinly-ai/joinly:latest-cuda -v --client <MeetingURL>
```

By default, the `joinly` image uses the Whisper model `base` for transcription, since it still runs reasonably fast on CPU. For `cuda`, it automatically defaults to `distil-large-v3` for significantly better transcription quality. You can change the model by setting `--stt-arg model_name=<model_name>` (e.g., `--stt-arg model_name=large-v3`). However, only the respective default models are packaged in the docker image, so it will start to download the model weights on container start.

# :test_tube: Create your own agent

You can also write your own agent and connect it to our joinly MCP server. See the [code examples](https://github.com/joinly-ai/joinly/client/README.md#code-usage) for the joinly-client package or the [client_example.py](examples/client_example.py) if you want a starting point that doesn't depend on our framework.

The joinly MCP server provides following tools and resources:

### Tools

- **`join_meeting`** - Join meeting with URL, participant name, and optional passcode
- **`leave_meeting`** - Leave the current meeting
- **`speak_text`** - Speak text using TTS (requires `text` parameter)
- **`send_chat_message`** - Send chat message (requires `message` parameter)
- **`mute_yourself`** - Mute microphone
- **`unmute_yourself`** - Unmute microphone
- **`get_chat_history`** - Get current meeting chat history in JSON format
- **`get_participants`** - Get current meeting participants in JSON format
- **`get_transcript`** - Get current meeting transcript in JSON format, optionally filtered by minutes
- **`get_video_snapshot`** - Get an image from the current meeting, e.g., view a current screenshare

### Resources

- **`transcript://live`** - Live meeting transcript in JSON format, including timestamps and speaker information. Subscribable for real-time updates when new utterances are added.

# :building_construction: Developing joinly.ai

For development we recommend using the development container, which installs all necessary dependencies. To get started, install the DevContainer Extension for Visual Studio Code, open the repository and choose **Reopen in Container**.

<img src="https://raw.githubusercontent.com/joinly-ai/assets/main/images/others/reopen_in_container.png" width="500" alt="Reopen in Devcontainer">

The installation can take some time, since it downloads all packages as well as models for Whisper/Kokoro and the Chromium browser. At the end, it automatically invokes the [download_assets.py](scripts/download_assets.py) script. If you see errors like `Missing kokoro-v1.0.onnx`, run this script manually using:

```bash
uv run scripts/download_assets.py
```

We'd love to see what you are using it for or building with it. Showcase your work on our [discord](https://discord.com/invite/AN5NEBkS4d)

# :pencil2: Roadmap

**Meeting**

- [x] Meeting chat access
- [ ] Camera in video call with status updates
- [ ] Enable screen share during video conferences
- [ ] Participant metadata and joining/leaving
- [ ] Improve browser agent capabilities

**Conversation**

- [x] Speaker attribute for transcription
- [ ] Improve client memory: reduce token usage, allow persistence across meetings
      events
- [ ] Improve End-of-Utterance/turn-taking detection
- [ ] Human approval mechanism from inside the meeting

**Integrations**

- [ ] Showcase how to add agents using the A2A protocol
- [ ] Add more provider integrations (STT, TTS)
- [ ] Integrate meeting platform SDKs
- [ ] Add alternative open-source meeting provider
- [ ] Add support for Speech2Speech models

# :busts_in_silhouette: Contributing

Contributions are always welcome! Feel free to open issues for bugs or submit a feature request. We'll do our best to review all contributions promptly and help merge your changes.

Please check our [Roadmap](#pencil2-roadmap) and don't hesitate to reach out to us!

# :memo: License

This project is licensed under the MIT License ‒ see the [LICENSE](LICENSE) file for details.

# :speech_balloon: Getting help

If you have questions or feedback, or if you would like to chat with the maintainers or other community members, please use the following links:

- [Join our Discord](https://discord.com/invite/AN5NEBkS4d)
- [Explore our GitHub Discussions](https://github.com/joinly-ai/joinly/discussions)

<div align="center">
Made with ❤️ in Osnabrück
 </div>
