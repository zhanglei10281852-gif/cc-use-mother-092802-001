"""可注入时钟：服务运行用系统时钟，自动化验证用手动时钟，保证离线可复现。"""

from datetime import datetime, timedelta, timezone


class Clock:
    """时钟接口。"""

    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    """真实系统时钟（UTC）。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock(Clock):
    """手动时钟，供测试与端到端验证推进时间。"""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("ManualClock 需要带时区的起始时间")
        self._current = start

    def now(self) -> datetime:
        return self._current

    def set(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("需要带时区的时间")
        self._current = instant

    def advance(self, **kwargs) -> datetime:
        self._current += timedelta(**kwargs)
        return self._current
