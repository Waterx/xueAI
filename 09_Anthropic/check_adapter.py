"""通过运行中的 /chat 验收真实流式 API，会消耗少量 DeepSeek API 额度。"""

import argparse
import asyncio
import json
import time

import httpx


async def main(url: str):
    events = []
    texts = []
    started = time.monotonic()
    event_type = None
    async with httpx.AsyncClient(timeout=90.0) as client:
        async with client.stream("POST", url.rstrip("/") + "/chat", json={
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "请用三句话介绍流式输出，每句话不超过二十字。"}],
        }) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    events.append(event_type)
                    print(f"{time.monotonic() - started:.2f}s {event_type}: {data}", flush=True)
                    if event_type == "error":
                        raise RuntimeError(f"接口返回 error：{data}")
                    if event_type == "delta":
                        texts.append(data["text"])
                    if event_type == "done" and data.get("reason") != "stop":
                        raise RuntimeError(f"回答未正常完成：{data}")
    if not events or events[0] != "start" or events[-1] != "done":
        raise RuntimeError(f"事件顺序异常：{events}")
    if len(texts) < 2 or not "".join(texts).strip():
        raise RuntimeError("未收到连续文本增量")
    print(f"验收通过：收到 start、{len(texts)} 个 delta、done。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8005")
    asyncio.run(main(parser.parse_args().url))
