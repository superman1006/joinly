import logging
from contextvars import ContextVar, Token

from joinly.types import Usage

logger = logging.getLogger(__name__)


_current_usage: ContextVar[Usage] = ContextVar("current_usage", default=Usage())  # noqa: B039


def get_usage() -> Usage:
    """获取当前用量统计。

    返回:
        Usage: 当前用量统计。
    """
    return _current_usage.get()


def set_usage(usage: Usage) -> Token[Usage]:
    """设置当前用量统计。

    参数:
        usage: 要设置的用量统计。

    返回:
        Token[Usage]: 可用于通过 `reset_usage` 恢复先前统计的令牌。
    """
    return _current_usage.set(usage)


def reset_usage(token: Token[Usage]) -> None:
    """将当前用量统计恢复为上一值。

    参数:
        token: `set_usage` 返回的令牌。
    """
    _current_usage.reset(token)


def add_usage(
    service: str,
    usage: dict[str, int | float],
    meta: dict[str, str | int | float] | None = None,
) -> None:
    """为某服务累加用量统计。

    参数:
        service: 服务名称。
        usage: 用量统计字典。
        meta: 与用量相关的附加元数据。
    """
    current_usage = get_usage()
    current_usage.add(service, usage=usage, meta=meta)
