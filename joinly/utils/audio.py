import numpy as np

from joinly.types import AudioFormat, IncompatibleAudioFormatError

BYTE_DEPTH_16 = 2
BYTE_DEPTH_32 = 4


def convert_audio_format(
    data: bytes, source_format: AudioFormat, target_format: AudioFormat
) -> bytes:
    """将音频数据从一种格式转换为另一种格式。

    参数:
        data: 表示音频数据的字节串。
        source_format: 表示源格式的 AudioFormat 对象。
        target_format: 表示目标格式的 AudioFormat 对象。

    返回:
        bytes: 转换为目标格式后的音频数据。

    引发:
        IncompatibleAudioFormatError: 当源格式与目标格式不兼容时。
    """
    if source_format.sample_rate != target_format.sample_rate:
        msg = (
            f"Incompatible sample rates: source={source_format.sample_rate}, "
            f"target={target_format.sample_rate}. "
            "Sample rate conversion is not supported."
        )
        raise IncompatibleAudioFormatError(msg)

    if source_format.byte_depth == target_format.byte_depth:
        return data

    if (
        source_format.byte_depth == BYTE_DEPTH_32
        and target_format.byte_depth == BYTE_DEPTH_16
    ):
        floats = np.frombuffer(data, dtype=np.float32)
        ints = np.clip(floats * 32767.0, -32768, 32767).astype(np.int16)
        return ints.tobytes()

    if (
        source_format.byte_depth == BYTE_DEPTH_16
        and target_format.byte_depth == BYTE_DEPTH_32
    ):
        ints = np.frombuffer(data, dtype=np.int16)
        floats = ints.astype(np.float32) / 32767.0
        return floats.tobytes()

    msg = (
        f"Incompatible byte depths: source={source_format.byte_depth}, "
        f"target={target_format.byte_depth}. "
        "Only conversion between 16-bit and 32-bit PCM is supported."
    )
    raise IncompatibleAudioFormatError(msg)


def calculate_audio_duration_ns(byte_size: int, audio_format: AudioFormat) -> int:
    """根据字节大小计算音频时长（纳秒）。

    参数:
        byte_size: 音频数据的字节长度。
        audio_format: 包含采样率与字节深度的 AudioFormat 对象。

    返回:
        int: 音频时长（纳秒）。
    """
    return (
        byte_size // audio_format.byte_depth * 1_000_000_000 // audio_format.sample_rate
    )


def calculate_audio_duration(byte_size: int, audio_format: AudioFormat) -> float:
    """根据字节大小计算音频时长（秒）。

    参数:
        byte_size: 音频数据的字节长度。
        audio_format: 包含采样率与字节深度的 AudioFormat 对象。

    返回:
        float: 音频时长（秒）。
    """
    return byte_size / (audio_format.sample_rate * audio_format.byte_depth)
