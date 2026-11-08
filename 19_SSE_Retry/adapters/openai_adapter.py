"""使用 OpenAI 兼容协议直接调用 DeepSeek Flash。"""

import os
from typing import Any
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from adapters.base import BaseLLMAdapter
from output_monitor import OutputMonitor
from schemas import LLMEvent, LLMResult


class OpenAIAdapter(BaseLLMAdapter):
    async def complete(
        self, messages: list[dict[str, str]], model: str,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResult:
        """历史兼容适配器未注册；本关非流式调用使用另两个适配器。"""
        raise NotImplementedError("请通过 Registry 使用 AnthropicAdapter 或 ResponsesAdapter")

    async def stream(
        self, messages: list[dict[str, str]], model: str
    ) -> AsyncIterator[LLMEvent]:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY 环境变量")
        monitor = OutputMonitor(model)

        # 请求取消或生成结束时，上游流和 HTTP 客户端都会关闭。
        async with AsyncOpenAI(
            api_key=key,
            base_url="https://api.deepseek.com",
            timeout=60.0,
            max_retries=0,
        ) as client:
            async with client.chat.completions.stream(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一位友好的助手，请用中文简洁回答。"},
                    *messages,
                ],
                max_tokens=1024,
                stream_options={"include_usage": True},
                extra_body={"thinking": {"type": "disabled"}},
            ) as stream:
                async for event in stream:
                    if event.type == "chunk":
                        monitor.completion_id = event.chunk.id
                    if event.type == "content.delta":
                        monitor.check(event.delta)
                        yield LLMEvent(type="delta", text=event.delta)
                # 已遍历完上游流，快照包含最终文本、完成原因及用量。
                # get_final_completion() 的解析步骤会对 length/content_filter 抛错，
                # 此处需要保留这些原因作为普通 done 事件传给调用方。
                completion = stream.current_completion_snapshot
                reason = completion.choices[0].finish_reason
                # usage 在 done 之前发送，让收到 done 就结束读取的旧前端也能兼容。
                if completion.usage is not None:
                    yield LLMEvent(type="usage", usage={
                        "prompt_tokens": completion.usage.prompt_tokens,
                        "completion_tokens": completion.usage.completion_tokens,
                        "total_tokens": completion.usage.total_tokens,
                    })
                yield LLMEvent(type="done", finish_reason=reason)
