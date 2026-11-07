"""真实模型适配层：DeepSeek Flash → 新增文本。"""

import os
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from output_monitor import OutputMonitor


async def stream_reply(messages: list[dict[str, str]], model: str) -> AsyncIterator[str]:
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
            extra_body={"thinking": {"type": "disabled"}},
        ) as stream:
            async for event in stream:
                if event.type == "chunk":
                    monitor.completion_id = event.chunk.id
                if event.type == "content.delta":
                    monitor.check(event.delta)
                    yield event.delta
            completion = await stream.get_final_completion()
            reason = completion.choices[0].finish_reason
            if reason != "stop":
                raise RuntimeError(f"模型未正常完成：{reason}")
