# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

joinly is a Python middleware that enables AI agents to join and participate in video meetings (Google Meet, Zoom, Teams, Feishu). It exposes a FastMCP server providing meeting tools (join, leave, speak, transcribe, chat, snapshot, screen share) that any MCP-compatible AI client can use.

## Build & Development Commands

```bash
# Install all dependencies (run from repo root)
uv sync --frozen

# Download required ML models (Silero VAD, Whisper, Kokoro TTS)
uv run scripts/download_assets.py

# Lint (ruff checks all rules by default)
uv run ruff check .
uv run ruff check --fix .    # autofix

# Format
uv run ruff format .

# Type check
uv run pyright

# Run tests (skips manual tests by default)
uv run pytest

# Run a single test
uv run pytest tests/test_meeting_transcription.py::TestTranscription::test_mcp_transcription -v

# Run manual/e2e tests (requires JOINLY_TEST_MEETING_URL env var)
uv run pytest -m manual

# Start as MCP server
uv run joinly --port 8000

# Start as client (built-in agent joins a meeting directly)
uv run joinly --client <MeetingURL>

# GPU acceleration (install CUDA extras first)
uv sync --extra cuda
```

### Docker (quickstart)
```bash
docker pull ghcr.io/joinly-ai/joinly:latest
# Run as client
docker run --env-file .env ghcr.io/joinly-ai/joinly:latest --client <MeetingURL>
# Run as server (connect external client to port 8000)
docker run -p 8000:8000 --env-file .env ghcr.io/joinly-ai/joinly:latest
```
See `.env.example` for all environment variables (`JOINLY_` prefix, e.g. `JOINLY_LLM_MODEL`, `JOINLY_LLM_PROVIDER`).

### Docker（飞书专用镜像）

> **Python 版本约束**：Dockerfile 通过 `ENV UV_PYTHON=3.12` 固定使用 **Python 3.12**。
> 不可升级到 3.13+，因为 `onnxruntime==1.21.1` 目前只提供 cp312/cp313 的 wheel，3.14+ 会导致构建失败。
> 若将来升级 onnxruntime，请先确认新版本对目标 Python 版本有对应 wheel 再修改此约束。

```bash
# 首次构建（需 30-60 分钟，需开 VPN）
docker build --platform linux/amd64 -t joinly-feishu .

# 只改代码后快速重建（复用缓存，不重新下载任何内容）
docker build --platform linux/amd64 --cache-from joinly-feishu:latest -t joinly-feishu:latest .

# 运行飞书会议
docker run -d --name joinly-feishu \
  --env-file .env \
  -e JOINLY_FEISHU_COOKIES_FILE=/cookies/feishu_cookies.json \
  -v $(pwd)/feishu_cookies.json:/cookies/feishu_cookies.json:ro \
  joinly-feishu:latest \
  --client "https://vc.feishu.cn/j/<会议ID>"

# 查看日志
docker logs -f joinly-feishu
```

## Workspace Structure

This is a **uv workspace** with three packages:

| Package | Directory | PyPI name | Purpose |
|---|---|---|---|
| `joinly` | `joinly/` | `joinly` | Main MCP server + meeting automation |
| `joinly-client` | `client/joinly_client/` | `joinly-client` | Python client library + LLM conversational agent |
| `joinly-common` | `common/joinly_common/` | `joinly-common` | Shared Pydantic types used by both |

Workspace sources are linked locally via `[tool.uv.sources]`. Each sub-package has its own `pyproject.toml` and is versioned/released independently (tags: `v*`, `client-v*`, `common-v*`).

## Architecture

### Core Design Patterns

- **Protocol-based DI**: All major components (`STT`, `TTS`, `VAD`, `MeetingProvider`, controllers) are defined as `Protocol` classes in `joinly/core.py`. `SessionContainer` (`joinly/container.py`) resolves short string tokens (e.g. `"whisper"`) to implementations by convention (`joinly.services.stt.whisper.WhisperSTT`).
- **ContextVar per-session state**: `Settings` and `Usage` live in `ContextVar` so each MCP client connection gets isolated configuration. Settings can be overridden per-connection via the `joinly-settings` HTTP header.
- **EventBus pub/sub**: Two event types (`"segment"`, `"utterance"`) in `joinly/utils/events.py` loosely couple the transcription pipeline to MCP resource subscriptions.
- **MCP as the public API**: All meeting capabilities are MCP tools/resources defined in `joinly/server.py`. The client package connects via `StreamableHttpTransport` or directly to a `FastMCP` instance.

### Audio Pipeline

`AudioReader` → format conversion → `VAD.stream()` → utterance boundary detection → `STT.stream()` → `Transcript`. The `no_speech_event` (asyncio.Event) flows from `TranscriptionController` to `SpeechController` to enable barge-in/interruption.

### Key Modules

- **`joinly/server.py`** — MCP tool/resource definitions, health endpoint, session lifespan
- **`joinly/session.py`** — `MeetingSession` orchestrates provider + controllers
- **`joinly/container.py`** — DI container, builds `MeetingSession` from `Settings`
- **`joinly/settings.py`** — `Settings` (pydantic-settings, `JOINLY_` env prefix)
- **`joinly/controllers/`** — `DefaultTranscriptionController` (VAD→STT pipeline), `DefaultSpeechController` (text chunking→TTS→audio output with interruption support)
- **`joinly/services/`** — STT (`whisper`, `deepgram`, `google`), TTS (`kokoro`, `elevenlabs`, `deepgram`, `google`, `resemble`), VAD (`silero`, `webrtc`, `hybrid`)
- **`joinly/providers/browser/`** — Virtual AV stack: PulseAudio, Xvfb, Playwright Chromium. Platform controllers for Google Meet, Zoom, Teams, and Feishu in `providers/browser/platforms/`; each matches URLs via `url_pattern` regex.
- **`client/joinly_client/agent.py`** — `ConversationalToolAgent` built on `pydantic-ai` model_request, manages rolling message history and parallel tool execution
- **`client/joinly_client/prompts.py`** — System prompt templates (dyadic vs multi-party)
- **`common/joinly_common/types.py`** — `TranscriptSegment`, `Transcript`, `VideoSnapshot`, `MeetingParticipant`, `Usage`, etc.

## Code Style & Conventions

- **Ruff**: `select = ["ALL"]` with specific ignores. Google docstring convention. Line length 88.
- **Pyright**: `typeCheckingMode = "standard"` across all packages.
- **Pre-commit hooks**: `ruff` (lint+fix), `ruff-format`, `uv-lock` (keeps lockfile in sync).
- **Tests**: pytest with `asyncio_mode = "auto"`, session-scoped event loop. Tests marked `manual` require a real meeting URL. Integration tests use a mockup browser meeting serving pre-recorded audio.
- **Commit style**: Conventional commits (e.g. `feat(client):`, `fix:`, `refactor:`, `test:`).
- **Docstrings**: Existing docstrings in `joinly/core.py` and other modules are written in Chinese (the project is developed by a Chinese team). Match the language of surrounding code when adding new docstrings.
