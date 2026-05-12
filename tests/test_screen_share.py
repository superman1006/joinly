"""Manual screen sharing tests.

These tests require a real meeting URL and human interaction (admitting the
bot into the meeting).  They are excluded from the default pytest run and can
be invoked explicitly::

    JOINLY_TEST_MEETING_URL="https://..." \
        uv run pytest -m manual tests/test_screen_share.py

To test against a running joinly server (e.g. Docker) instead of in-process::

    JOINLY_TEST_MEETING_URL="https://..." JOINLY_TEST_URL="http://localhost:8000/mcp" \
        uv run pytest -m manual tests/test_screen_share.py
"""

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from fastmcp import Client

from joinly.settings import Settings, set_settings

MEETING_URL = os.environ.get("JOINLY_TEST_MEETING_URL")
JOINLY_TEST_URL = os.environ.get("JOINLY_TEST_URL")
SHARE_URL = os.environ.get(
    "JOINLY_TEST_SHARE_URL",
    "https://en.wikipedia.org/wiki/Screen_sharing",
)

pytestmark = pytest.mark.manual


@pytest.fixture(scope="module", autouse=True)
def _settings() -> None:
    """为手动测试配置最小化设置。"""
    if not JOINLY_TEST_URL:
        set_settings(Settings(name="joinly", vad="webrtc", stt="whisper", tts="kokoro"))


@pytest.fixture(scope="module")
async def client() -> AsyncIterator[Client]:
    """为本测试模块创建已连接的 MCP 客户端。"""
    if JOINLY_TEST_URL:
        async with Client(JOINLY_TEST_URL) as c:
            yield c
    else:
        from joinly.server import mcp

        async with Client(mcp) as c:
            yield c


@pytest.mark.skipif(not MEETING_URL, reason="JOINLY_TEST_MEETING_URL not set")
async def test_share_screen(client: Client) -> None:
    """加入会议、共享 URL、停止共享并离开。"""
    await client.call_tool(
        "join_meeting",
        arguments={"meeting_url": MEETING_URL},
    )

    await asyncio.sleep(15)

    await client.call_tool(
        "share_screen",
        arguments={"url": SHARE_URL},
    )

    await asyncio.sleep(30)

    await client.call_tool("stop_sharing")
    await client.call_tool("leave_meeting")
