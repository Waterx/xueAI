"""通过运行中的 /chat 验收两种模型 × 两种模式，会消耗少量 API 额度。"""

import argparse
import asyncio
import json
import time

import httpx


async def check_model(url: str, model: str):
    events = []
    texts = []
    started = time.monotonic()
    event_type = None
    async with httpx.AsyncClient(timeout=90.0) as client:
        async with client.stream("POST", url.rstrip("/") + "/chat", json={
            "model": model,
            "stream": True,
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
    if events.count("usage") != 1 or events.count("done") != 1:
        raise RuntimeError(f"缺少唯一的 usage / done：{events}")
    if events.index("usage") > events.index("done"):
        raise RuntimeError("usage 应先于 done")
    if len(texts) < 2 or not "".join(texts).strip():
        raise RuntimeError("未收到连续文本增量")
    print(f"{model} stream=true 验收通过：收到 start、{len(texts)} 个 delta、done。")


async def check_complete(url: str, model: str):
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(url.rstrip("/") + "/chat", json={
            "model": model, "stream": False,
            "messages": [{"role": "user", "content": "请用三句话介绍流式输出，每句话不超过二十字。"}],
        })
        response.raise_for_status()
    if "application/json" not in response.headers.get("content-type", ""):
        raise RuntimeError("非流式调用未返回 JSON")
    result = response.json()
    if (result.get("model") != model or result.get("finish_reason") != "stop"
            or not isinstance(result.get("content"), str) or not result["content"].strip()):
        raise RuntimeError(f"非流式结果异常：{result}")
    usage = result.get("usage")
    if (not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
        raise RuntimeError(f"用量异常：{usage}")
    print(f"{model} stream=false 验收通过：{json.dumps(result, ensure_ascii=False)}", flush=True)


async def main(url: str):
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        await check_model(url, model)
        await check_complete(url, model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8013")
    asyncio.run(main(parser.parse_args().url))
