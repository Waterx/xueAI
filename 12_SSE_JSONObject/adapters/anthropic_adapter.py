"""使用 Anthropic Messages 协议调用 DeepSeek Flash。"""

import os
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from adapters.base import BaseLLMAdapter
from output_monitor import OutputMonitor
from schemas import LLMEvent, LLMResult


class AnthropicAdapter(BaseLLMAdapter):
    async def complete(
        self, messages: list[dict[str, str]], model: str,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")
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
            system += "\n\n请只返回一个合法 JSON 对象，不包含 Markdown 代码块、解释或其他文字。"
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
                raise RuntimeError(f"模型未正常完成：{message.stop_reason!r}")
            content = "".join(block.text for block in message.content if block.type == "text")
            monitor = OutputMonitor(model)
            monitor.completion_id = message.id
            monitor.check(content)
            usage = message.usage
            return LLMResult(
                model=model, content=content, finish_reason=reason,
                usage=None if usage is None else {
                    "prompt_tokens": usage.input_tokens,
                    "completion_tokens": usage.output_tokens,
                    "total_tokens": usage.input_tokens + usage.output_tokens,
                },
            )

    async def stream(
        self, messages: list[dict[str, str]], model: str
    ) -> AsyncIterator[LLMEvent]:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")
        monitor = OutputMonitor(model)

        async with AsyncAnthropic(
            api_key=key,
            base_url="https://api.deepseek.com/anthropic",
            timeout=60.0,
            max_retries=0,
        ) as client:
            async with client.messages.stream(
                model=model,
                messages=messages,
                system="你是一位友好的助手，请用中文简洁回答。",
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
                    raise RuntimeError(f"模型未正常完成：{message.stop_reason!r}")

                usage = message.usage
                if usage is not None:
                    yield LLMEvent(type="usage", usage={
                        "prompt_tokens": usage.input_tokens,
                        "completion_tokens": usage.output_tokens,
                        "total_tokens": usage.input_tokens + usage.output_tokens,
                    })
                yield LLMEvent(type="done", finish_reason=reason)
