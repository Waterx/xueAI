"""真实 API 验收：需要 DEEPSEEK_API_KEY，会消耗少量 API 额度。"""

import asyncio

from adapters.openai_adapter import OpenAIAdapter


async def main():
    events = []
    async for event in OpenAIAdapter().stream(
        [{"role": "user", "content": "请只回复：你好"}], "deepseek-flash"
    ):
        events.append(event)
        print(event.model_dump(exclude_none=True))

    if not any(event.type == "delta" for event in events):
        raise RuntimeError("未收到文字事件")
    if not events or events[-1].type != "done":
        raise RuntimeError("未收到结束事件")
    if events[-1].finish_reason != "stop":
        raise RuntimeError(f"回答未正常完成：{events[-1].finish_reason}")


if __name__ == "__main__":
    asyncio.run(main())
