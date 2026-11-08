"""使用 OpenAI Responses 协议调用 DeepSeek Pro。"""

import os
from typing import Any
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from adapters.base import BaseLLMAdapter
from errors import UpstreamResponseError
from output_monitor import OutputMonitor
from schemas import LLMEvent, LLMResult


def _prepare_request(
    messages: list[dict[str, str]],
    response_format: dict[str, Any] | None,
) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    systems = []
    inputs = []
    for message in messages:
        role = message["role"]
        if role == "system":
            systems.append(message["content"])
        elif role in ("user", "assistant"):
            # Responses 的简易 message 格式支持字符串形式的历史内容。
            inputs.append({"role": role, "content": message["content"]})
        else:
            raise ValueError(f"不支持的消息角色：{role!r}")
    instructions = "\n\n".join(systems) if systems else "你是一位友好的助手，请用中文简洁回答。"

    options = {}
    if response_format is not None:
        options["text"] = {"format": response_format}
        if response_format["type"] == "json_schema":
            instructions += "\n\n请只返回严格符合指定 JSON Schema 的合法 JSON，不要包含 Markdown 代码块。"
        else:
            instructions += "\n\n请只返回一个合法 JSON 对象，不要包含 Markdown 代码块。"
    return instructions, inputs, options


class ResponsesAdapter(BaseLLMAdapter):
    async def complete(
        self, messages: list[dict[str, str]], model: str,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResult:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")
        instructions, inputs, options = _prepare_request(messages, response_format)

        async with AsyncOpenAI(
            api_key=key, base_url="https://api.deepseek.com",
            timeout=60.0, max_retries=0,
        ) as client:
            response = await client.responses.create(
                model=model, instructions=instructions, input=inputs,
                max_output_tokens=1024, reasoning={"effort": "none"}, stream=False,
                **options,
            )
            if (response.status != "completed" or response.error is not None
                    or response.incomplete_details is not None):
                raise UpstreamResponseError(
                    f"模型未正常完成：{response.status!r}", getattr(response.error, "code", None),
                )
            content = response.output_text
            monitor = OutputMonitor(model)
            monitor.completion_id = response.id
            monitor.check(content)
            usage = response.usage
            return LLMResult(
                model=model, content=content, finish_reason="stop",
                usage=None if usage is None else {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens,
                },
            )

    async def stream(
        self, messages: list[dict[str, str]], model: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")

        instructions, inputs, options = _prepare_request(messages, response_format)
        monitor = OutputMonitor(model)

        async with AsyncOpenAI(
            api_key=key,
            base_url="https://api.deepseek.com",
            timeout=60.0,
            max_retries=0,
        ) as client:
            stream = await client.responses.create(
                model=model,
                instructions=instructions,
                input=inputs,
                max_output_tokens=1024,
                reasoning={"effort": "none"},
                stream=True,
                **options,
            )
            async with stream:
                async for event in stream:
                    if event.type == "response.created":
                        monitor.completion_id = event.response.id
                    elif event.type == "response.output_text.delta":
                        monitor.check(event.delta)
                        yield LLMEvent(type="delta", text=event.delta)
                    elif event.type == "response.completed":
                        response = event.response
                        if (response.status != "completed" or response.error is not None
                                or response.incomplete_details is not None):
                            raise UpstreamResponseError(
                                f"模型未正常完成：{response.status!r}", getattr(response.error, "code", None),
                            )
                        usage = response.usage
                        if usage is not None:
                            yield LLMEvent(type="usage", usage={
                                "prompt_tokens": usage.input_tokens,
                                "completion_tokens": usage.output_tokens,
                                "total_tokens": usage.input_tokens + usage.output_tokens,
                            })
                        yield LLMEvent(type="done", finish_reason="stop")
                        return
                    elif event.type == "response.failed":
                        raise UpstreamResponseError(
                            f"模型未正常完成：{event.type}", getattr(event.response.error, "code", None),
                        )
                    elif event.type == "error":
                        raise UpstreamResponseError(f"模型未正常完成：{event.type}", event.code)
                    elif event.type == "response.incomplete":
                        raise UpstreamResponseError(f"模型未正常完成：{event.type}")
                raise UpstreamResponseError("模型未正常完成：流结束时缺少 response.completed")
