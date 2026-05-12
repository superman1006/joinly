import asyncio
import fcntl
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Self

from joinly.core import AudioReader
from joinly.providers.browser.devices.pulse_module_manager import (
    PulseModuleManager,
)
from joinly.types import AudioChunk, AudioFormat

logger = logging.getLogger(__name__)

_ENV_VAR = "PULSE_SINK"


class VirtualSpeaker(PulseModuleManager, AudioReader):
    """创建并卸载虚拟音频空接收端（null sink）的类。"""

    def __init__(  # noqa: PLR0913
        self,
        *,
        sample_rate: int = 16000,
        frames_per_chunk: int = 512,
        byte_depth: int = 4,
        pipe_size: int | None = None,
        fifo_path: Path | None = None,
        sink_name: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """初始化 VirtualSpeaker。

        参数:
            sample_rate (int): 音频流采样率（默认 16000）。
            frames_per_chunk (int): 每个块的帧数（默认 512）。
            byte_depth (int): 采样位深（默认 4）。
            pipe_size (int): 音频管道缓冲区大小（默认 4096）。
            fifo_path (Path | None): FIFO 文件路径（默认 None）。
            sink_name (str | None): sink 名称（默认 None）。
            env: 用于设置 sink 名称的可选环境变量字典。
        """
        if byte_depth not in (2, 4):
            msg = f"Invalid byte depth: {byte_depth}. Must be 2 or 4."
            raise ValueError(msg)
        self.audio_format = AudioFormat(sample_rate=sample_rate, byte_depth=byte_depth)
        self.frames_per_chunk = frames_per_chunk
        self.fifo_path = fifo_path
        self.sink_name: str = (
            sink_name if sink_name is not None else f"virt.{uuid.uuid4()}"
        )
        self.chunk_size = frames_per_chunk * self.audio_format.byte_depth
        self.pipe_size = pipe_size if pipe_size is not None else self.chunk_size * 2
        self._pulse_format = "float32le" if byte_depth == 4 else "s16le"  # noqa: PLR2004
        self._env: dict[str, str] = env if env is not None else {}
        self._dir: tempfile.TemporaryDirectory[str] | None = None
        self._module_id: int | None = None
        self._reader: asyncio.StreamReader | None = None
        self._time_ns: int = 0
        self._chunk_ns: int = frames_per_chunk * 1_000_000_000 // sample_rate

    async def __aenter__(self) -> Self:
        """创建虚拟音频 sink 并开始采集。

        引发:
            RuntimeError: 当 sink 创建失败时。
        """
        if self._module_id is not None:
            msg = "Audio sink already created"
            raise RuntimeError(msg)

        if self._reader is not None:
            msg = "Audio reader already started"
            raise RuntimeError(msg)

        if self.fifo_path is None:
            self._dir = tempfile.TemporaryDirectory(prefix="virtsink_")
            self.fifo_path = Path(self._dir.name) / "fifo.pcm"
        elif self.fifo_path.exists():
            msg = f"FIFO file already exists: {self.fifo_path}"
            logger.error(msg)
            raise RuntimeError(msg)

        logger.debug("Creating FIFO file: %s", self.fifo_path)
        os.mkfifo(self.fifo_path, 0o600)

        logger.debug("Creating virtual audio sink: %s", self.sink_name)
        self._module_id = await self._load_module(
            "module-pipe-sink",
            f"sink_name={self.sink_name}",
            f"file={self.fifo_path}",
            f"rate={self.audio_format.sample_rate}",
            f"format={self._pulse_format}",
            "channels=1",
            "use_system_clock_for_timing=yes",
            env=self._env,
        )
        logger.debug(
            "Created virtual audio sink: %s (id: %s)",
            self.sink_name,
            self._module_id,
        )

        logger.debug("Setting up FIFO file for reading: %s", self.fifo_path)
        fd = os.open(self.fifo_path, os.O_RDWR | os.O_NONBLOCK)
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, self.pipe_size)

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        await loop.connect_read_pipe(lambda: protocol, os.fdopen(fd, "rb", buffering=0))
        self._reader = reader

        self._env[_ENV_VAR] = self.sink_name
        self._time_ns = 0

        logger.debug(
            "Virtual speaker is ready (sink: %s, id: %s, fifo: %s, rate: %s)",
            self.sink_name,
            self._module_id,
            self.fifo_path,
            self.audio_format.sample_rate,
        )

        return self

    async def __aexit__(self, *_exc: object) -> None:
        """卸载 sink 模块。"""
        if self._reader is None:
            logger.warning("No FIFO file to close")
        else:
            logger.debug("Closing FIFO file: %s", self.fifo_path)
            self._reader.feed_eof()
            self._reader = None

        if self._module_id is None:
            logger.warning("No module ID found, skipping unload.")
        else:
            logger.debug(
                "Unloading virtual audio sink: %s (id: %s)",
                self.sink_name,
                self._module_id,
            )

            await self._unload_module(self._module_id, env=self._env)

            if self._env.get(_ENV_VAR) == self.sink_name:
                self._env.pop(_ENV_VAR)

            logger.debug(
                "Unloaded virtual audio sink: %s (id: %s)",
                self.sink_name,
                self._module_id,
            )
            self._module_id = None

        if self._dir is not None:
            self._dir.cleanup()
            logger.debug("Temporary directory removed: %s", self._dir.name)
            self._dir = None
        elif self.fifo_path is not None:
            self.fifo_path.unlink()
            logger.debug("FIFO file removed: %s", self.fifo_path)
            self.fifo_path = None
        else:
            logger.warning("No FIFO file to remove")

    async def read(self) -> AudioChunk:
        """从流中返回下一块音频数据。

        返回:
            AudioChunk: Audio data in f32le format with specified sample rate.
        """
        if self._reader is None:
            msg = "Audio reader not started"
            raise RuntimeError(msg)

        chunk = AudioChunk(
            data=await self._reader.readexactly(self.chunk_size),
            time_ns=self._time_ns,
        )
        self._time_ns += self._chunk_ns
        return chunk
