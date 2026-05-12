import contextlib
import mimetypes
from collections.abc import AsyncGenerator
from pathlib import Path

import aiofiles
import aiohttp.web


def _create_mockup_meeting_html() -> str:
    """创建用于播放音频的 HTML 模板。"""
    return """
    <!DOCTYPE html>
    <html>
    <body>
    <input id="name" type="text" placeholder="Enter your name">
    <button id="join">Join</button>
    <button id="leave">Leave</button>
    <audio id="audio" src="/speech_audio"></audio>
    <script>
      document.getElementById('join').addEventListener('click', () => {
        document.getElementById('audio').play().catch(() => {{}});
      });
    </script>
    </body>
    </html>
    """


@contextlib.asynccontextmanager
async def serve_mockup_browser_meeting(
    speech_file_path: Path,
) -> AsyncGenerator[str, None]:
    """启动临时 HTTP 服务，提供用于测试的会议页面模拟。

    会创建临时 HTTP 服务，暴露两个端点：
    - 根路径 "/" 返回带音频播放器与模拟按钮的 HTML 页面
    - "/speech_audio" 返回指定音频文件内容

    参数:
        speech_file_path (Path): 要提供的音频文件路径

    产生:
        str: 临时服务 URL（例如 "http://127.0.0.1:{port}/"）

    引发:
        ValueError: 当指定文件不存在或类型不支持时
    """
    mime_type, _ = mimetypes.guess_type(speech_file_path)
    if mime_type is None or not mime_type.startswith("audio/"):
        msg = f"Unsupported file type: {speech_file_path}"
        raise ValueError(msg)

    try:
        async with aiofiles.open(speech_file_path, "rb") as f:
            speech_data = await f.read()
    except FileNotFoundError as err:
        msg = f"Speech file not found: {speech_file_path}"
        raise ValueError(msg) from err

    app = aiohttp.web.Application()

    async def handle_index(_request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(
            text=_create_mockup_meeting_html(), content_type="text/html"
        )

    async def handle_speech(_request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.Response(body=speech_data, content_type=mime_type)

    app.router.add_get("/", handle_index)
    app.router.add_get("/speech_audio", handle_speech)

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()

    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    host, port = runner.addresses[0]
    url = f"http://{host}:{port}/"

    try:
        yield url
    finally:
        await runner.cleanup()
