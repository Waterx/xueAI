"""兼容现有 HTTP 层的模型入口，具体调用由适配器完成。"""

import json

from collections.abc import AsyncIterator
from contextlib import aclosing

from adapters.registry import get_adapter
from schemas import LLMEvent, LLMResult


class StructuredOutputError(ValueError):
    """模型未返回完整、合法的 JSON 对象。"""


def reject_constant(value: str):
    raise ValueError(f"JSON 不允许常量 {value}")


async def complete_reply(
    messages: list[dict[str, str]], model: str,
    response_format: dict[str, str] | None = None,
) -> LLMResult:
    if response_format is not None and response_format != {"type": "json_object"}:
        raise ValueError('response_format 只支持 {"type":"json_object"}')
    adapter = get_adapter(model)
    if response_format is None:
        return await adapter.complete(messages, model)
    result = await adapter.complete(messages, model, response_format=response_format)
    if result.finish_reason == "length":
        raise StructuredOutputError("结构化输出被截断（finish_reason=length）")
    try:
        parsed = json.loads(result.content, parse_constant=reject_constant)
    except ValueError as exc:
        raise StructuredOutputError("结构化输出不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise StructuredOutputError("结构化输出必须是 JSON 对象")
    return result


async def stream_reply(messages: list[dict[str, str]], model: str) -> AsyncIterator[LLMEvent]:
    adapter = get_adapter(model)
    async with aclosing(adapter.stream(messages, model)) as events:
        async for event in events:
            yield event
