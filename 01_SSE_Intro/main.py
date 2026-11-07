"""FastAPI SSE demo。运行：python -m uvicorn main:app --reload"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse

app = FastAPI(title="SSE 入门 Demo")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/events")
async def events(request: Request):
    # 重连时继续编号；本例不保存或补发历史消息。
    try:
        sequence = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        sequence = 0

    async def generate():
        nonlocal sequence
        # retry 指定自动重连间隔（毫秒）。
        yield ": connected\nretry: 2000\n\n"
        try:
            while not await request.is_disconnected():
                sequence += 1

                data = json.dumps({
                    "number": sequence,
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "message": f"服务器推送第 {sequence} 条消息",
                }, ensure_ascii=False)

                # 每条 SSE 事件以空行结束。
                yield f"id: {sequence}\nevent: tick\ndata: {data}\n\n"
                await asyncio.sleep(1)
        finally:
            # 框架处理连接断开；在此释放订阅、上游连接等资源。
            print("SSE 客户端已断开", flush=True)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
