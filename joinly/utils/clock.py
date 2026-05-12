class Clock:
    """以纳秒为单位跟踪时间的简单时钟类。"""

    __slots__ = ("_time_ns",)

    def __init__(self) -> None:
        """将时钟初始化为从 0 纳秒开始。"""
        self._time_ns = 0

    def update(self, ns: int) -> None:
        """用新的纳秒时间更新时钟。

        参数:
            ns (int): 要设置的新时间（纳秒）。

        引发:
            ValueError: 当新时间小于当前时间时。
        """
        if ns >= self._time_ns:
            self._time_ns = ns
        else:
            msg = (
                f"Cannot update clock with {ns} ns, current time is {self._time_ns} ns"
                f" ({ns} < {self._time_ns})"
            )
            raise ValueError(msg)

    @property
    def now_ns(self) -> int:
        """获取当前时间（纳秒）。"""
        return self._time_ns

    @property
    def now_s(self) -> float:
        """获取当前时间（秒）。"""
        return self._time_ns / 1_000_000_000
