"""兼容现有 HTTP 层的模型入口，具体调用由适配器完成。"""

import json

from jsonschema import Draft202012Validator, ValidationError, validate

from typing import Any
from collections.abc import AsyncIterator
from contextlib import aclosing

from adapters.registry import get_adapter
from schemas import LLMEvent, LLMResult


class StructuredOutputError(ValueError):
    """模型未返回完整、合法且符合约束的 JSON。"""


def reject_constant(value: str):
    raise ValueError(f"JSON 不允许常量 {value}")


async def complete_reply(
    messages: list[dict[str, str]], model: str,
    response_format: dict[str, Any] | None = None,
) -> LLMResult:
    if response_format is not None:
        if response_format.get("type") not in ("json_object", "json_schema"):
            raise ValueError("response_format 只支持 json_object 和 json_schema")
        if response_format["type"] == "json_schema":
            if not response_format.get("name") or "schema" not in response_format:
                raise ValueError("json_schema 必须提供 name 和 schema")
            Draft202012Validator.check_schema(response_format["schema"])
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
    if response_format["type"] == "json_object" and not isinstance(parsed, dict):
        raise StructuredOutputError("结构化输出必须是 JSON 对象")
    if response_format["type"] == "json_schema":
        try:
            validate(instance=parsed, schema=response_format["schema"], cls=Draft202012Validator)
        except ValidationError as exc:
            raise StructuredOutputError("模型输出不符合 JSON Schema") from exc
    return result


async def stream_reply(messages: list[dict[str, str]], model: str) -> AsyncIterator[LLMEvent]:
    adapter = get_adapter(model)
    async with aclosing(adapter.stream(messages, model)) as events:
        async for event in events:
            yield event
