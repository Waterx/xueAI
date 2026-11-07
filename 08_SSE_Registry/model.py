"""兼容现有 HTTP 层的模型入口，具体调用由适配器完成。"""

from collections.abc import AsyncIterator
from contextlib import aclosing

from adapters.registry import get_adapter
from schemas import LLMEvent


async def stream_reply(messages: list[dict[str, str]], model: str) -> AsyncIterator[LLMEvent]:
    adapter = get_adapter(model)
    async with aclosing(adapter.stream(messages, model)) as events:
        async for event in events:
            yield event
