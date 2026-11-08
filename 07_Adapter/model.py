"""兼容现有 HTTP 层的模型入口，具体调用由适配器完成。"""

from collections.abc import AsyncIterator
from contextlib import aclosing

from adapters.openai_adapter import OpenAIAdapter
from schemas import LLMEvent


async def stream_reply(messages: list[dict[str, str]], model: str) -> AsyncIterator[LLMEvent]:
    async with aclosing(OpenAIAdapter().stream(messages, model)) as events:
        async for event in events:
            yield event
