"""请求级重试；SDK 重试保持关闭，避免叠加调用次数。"""

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

from anyio import CancelScope

from errors import normalize_error

T = TypeVar("T")
MAX_RETRIES = 3


async def wait_before_retry(retry_number: int) -> None:
    """retry_number 从 1 开始；等待可被任务取消立即打断。"""
    delay = min(5.0, 0.5 * 2 ** (retry_number - 1)) + random.uniform(0, 0.2)
    await asyncio.sleep(delay)


async def with_retry(operation: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt == MAX_RETRIES or not normalize_error(exc).retryable:
                raise
        await wait_before_retry(attempt + 1)


async def stream_with_retry(operation: Callable[[], AsyncIterator[T]]) -> AsyncIterator[T]:
    """仅捕获创建/读取上游的异常，yield 和关闭异常不进入重试逻辑。

    每次失败流先关闭再等待；任何事件（包括空 delta、usage、done）
    交给调用方后都保守地禁止重试。CancelledError/GeneratorExit 直接传播。
    """
    forwarded = False
    for attempt in range(MAX_RETRIES + 1):
        stream = None
        try:
            try:
                stream = operation()
            except Exception as exc:
                if attempt == MAX_RETRIES or not normalize_error(exc).retryable:
                    raise
            else:
                while True:
                    try:
                        event = await anext(stream)
                    except StopAsyncIteration:
                        return
                    except Exception as exc:
                        if forwarded or attempt == MAX_RETRIES or not normalize_error(exc).retryable:
                            raise
                        break
                    forwarded = True
                    yield event
        finally:
            if stream is not None:
                with CancelScope(shield=True):
                    await stream.aclose()
        await wait_before_retry(attempt + 1)
