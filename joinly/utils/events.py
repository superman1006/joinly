import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Literal

logger = logging.getLogger(__name__)

type EventType = Literal["segment", "utterance"]


class EventBus:
    """用于发布与订阅类型化事件的轻量级事件总线。"""

    def __init__(self) -> None:
        """初始化事件总线。"""
        self._listeners: dict[
            EventType, set[Callable[[], Coroutine[None, None, None]]]
        ] = {}

    def subscribe(
        self,
        event_type: EventType,
        handler: Callable[[], Coroutine[None, None, None]],
    ) -> Callable[[], None]:
        """订阅某类事件。

        参数:
            event_type: 要监听的事件类型。
            handler: 事件发生时调用的异步函数。

        返回:
            调用后可取消该处理器订阅的函数。
        """
        if event_type not in self._listeners:
            self._listeners[event_type] = set()

        self._listeners[event_type].add(handler)

        def unsubscribe() -> None:
            if event_type in self._listeners:
                self._listeners[event_type].discard(handler)
                if not self._listeners[event_type]:
                    del self._listeners[event_type]

        return unsubscribe

    def publish(self, event_type: EventType) -> None:
        """向所有订阅者发布事件。

        参数:
            event_type: 正在发布的事件类型。
        """
        if event_type not in self._listeners:
            return

        for handler in list(self._listeners[event_type]):
            asyncio.create_task(self._safe_call_handler(handler))  # noqa: RUF006

    async def _safe_call_handler(
        self,
        handler: Callable[[], Coroutine[None, None, None]],
    ) -> None:
        """安全调用事件处理器，并记录可能出现的异常。

        参数:
            handler: 要调用的处理器函数。
        """
        try:
            await handler()
        except Exception:
            logger.exception("Error in event handler: %s", handler)
