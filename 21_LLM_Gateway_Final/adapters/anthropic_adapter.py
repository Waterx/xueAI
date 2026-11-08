"""使用 Anthropic Messages 协议调用 DeepSeek Flash。"""

import json
import os
from typing import Any
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from adapters.base import BaseLLMAdapter
from errors import UpstreamResponseError
from output_monitor import OutputMonitor
from schemas import LLMEvent, LLMResult



def normalize_usage(usage) -> dict[str, Any] | None:
    """DeepSeek Messages 的三个输入分类互斥；缺失分类不推断为零。

    SDK 将最终 message_delta 的累计用量覆盖到最终 message，而非累加。
    保留供应商上报字段（含嵌套分类），仅新增统一统计字段。
    """
    if usage is None:
        return None
    raw = usage.model_dump(mode="json", exclude_unset=True)
    fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    counts = [raw.get(field) for field in fields]
    prompt = sum(counts) if all(type(n) is int and n >= 0 for n in counts) else None
    output = raw.get("output_tokens")
    if type(output) is not int or output < 0:
        output = None
    return {
        **raw,
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output if prompt is not None and output is not None else None,
    }


def _prepare_request(
    messages: list[dict[str, str]],
    response_format: dict[str, Any] | None,
) -> tuple[str, list[dict[str, str]]]:
    systems = []
    inputs = []
    for item in messages:
        if item["role"] == "system":
            systems.append(item["content"])
        elif item["role"] in ("user", "assistant"):
            inputs.append(dict(item))
        else:
            raise ValueError(f"不支持的消息角色：{item['role']!r}")
    system = "\n\n".join(systems) if systems else "你是一位友好的助手，请用中文简洁回答。"
    if response_format is not None:
        if response_format["type"] == "json_schema":
            system += (
                "\n\n请只返回一个严格符合以下 JSON Schema 的合法 JSON 值，"
                "不包含 Markdown 代码块、解释或其他文字。\nJSON Schema：\n"
                + json.dumps(response_format["schema"], ensure_ascii=False)
            )
        else:
            system += "\n\n请只返回一个合法 JSON 对象，不包含 Markdown 代码块、解释或其他文字。"
    return system, inputs


class AnthropicAdapter(BaseLLMAdapter):
    async def complete(
        self, messages: list[dict[str, str]], model: str,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResult:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")
        system, inputs = _prepare_request(messages, response_format)

        async with AsyncAnthropic(
            api_key=key, base_url="https://api.deepseek.com/anthropic",
            timeout=60.0, max_retries=0,
        ) as client:
            message = await client.messages.create(
                model=model, messages=inputs,
                system=system,
                max_tokens=1024, thinking={"type": "disabled"}, stream=False,
            )
            reasons = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}
            reason = reasons.get(message.stop_reason)
            if reason is None:
                raise UpstreamResponseError(f"模型未正常完成：{message.stop_reason!r}")
            content = "".join(block.text for block in message.content if block.type == "text")
            monitor = OutputMonitor(model)
            monitor.completion_id = message.id
            monitor.check(content)
            usage = message.usage
            return LLMResult(
                model=model, content=content, finish_reason=reason,
                usage=normalize_usage(usage),
            )

    async def stream(
        self, messages: list[dict[str, str]], model: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")
        system, inputs = _prepare_request(messages, response_format)
        monitor = OutputMonitor(model)

        async with AsyncAnthropic(
            api_key=key,
            base_url="https://api.deepseek.com/anthropic",
            timeout=60.0,
            max_retries=0,
        ) as client:
            async with client.messages.stream(
                model=model,
                messages=inputs,
                system=system,
                max_tokens=1024,
                thinking={"type": "disabled"},
            ) as stream:
                async for text in stream.text_stream:
                    monitor.check(text)
                    yield LLMEvent(type="delta", text=text)

                message = await stream.get_final_message()
                monitor.completion_id = message.id
                # 保留截断语义，未知结束原因不得伪装为正常结束。
                reasons = {"end_turn": "stop", "stop_sequence": "stop",
                           "max_tokens": "length"}
                reason = reasons.get(message.stop_reason)
                if reason is None:
                    raise UpstreamResponseError(f"模型未正常完成：{message.stop_reason!r}")

                usage = message.usage
                if usage is not None:
                    yield LLMEvent(type="usage", usage=normalize_usage(usage))
                yield LLMEvent(type="done", finish_reason=reason)
