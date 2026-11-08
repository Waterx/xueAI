"""模型适配层：接入真实模型时，替换此处的流式实现。"""

import asyncio
from collections.abc import AsyncIterator


async def stream_reply(prompt: str) -> AsyncIterator[str]:
    """逐段返回新增文本；伪 AI 不调用模型，也不需要 API Key。"""
    if prompt == "/error":
        await asyncio.sleep(0.3)
        raise RuntimeError("模拟模型调用失败")

    answer = (
        f"你问的是：{prompt}\n\n"
        "我是伪 AI，当前回答由固定模板生成，没有推理能力。\n\n"
        "这次请求先通过 POST 把问题交给后端，后端再把回答拆成小段，"
        "通过 SSE 持续发送。前端每收到一个 delta，就把新增文本追加到页面。\n\n"
        "接入真实模型时，只需让 model.py 逐段返回模型的新增文本，"
        "就能继续使用当前接口和前端。"
    )

    for offset in range(0, len(answer), 4):
        await asyncio.sleep(0.08)
        yield answer[offset:offset + 4]
