"""单进程、单事件循环内按模型独立的请求级滑动窗口。"""

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
import math
import time

from errors import RateLimitError

MODEL_LIMITS = {"deepseek-flash": 5, "deepseek-v4-pro": 3}
WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(
        self, limits: Mapping[str, int] | None = None,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._limits = dict(MODEL_LIMITS if limits is None else limits)
        if not self._limits or any(type(n) is not int or n <= 0 for n in self._limits.values()):
            raise ValueError("每个模型的限额必须是正整数")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("窗口长度必须是有限正数")
        self._window = window_seconds
        self._clock = clock
        self._timestamps: dict[str, deque[float]] = {model: deque() for model in self._limits}
        self._lock = asyncio.Lock()

    async def acquire(self, model: str) -> None:
        """立即放行并登记，或拒绝；失败、取消不退还已登记的请求额度。"""
        async with self._lock:
            timestamps = self._timestamps[model]
            now = self._clock()
            while timestamps and timestamps[0] <= now - self._window:
                timestamps.popleft()
            if len(timestamps) >= self._limits[model]:
                raise RateLimitError(math.ceil(max(0.0, self._window - (now - timestamps[0]))))
            timestamps.append(now)
