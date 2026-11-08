"""运行：../.venv/bin/python -m uvicorn main:app --reload --port 8001"""

import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from model import stream_reply

app = FastAPI(title="SSE 伪 AI Demo")
logger = logging.getLogger("uvicorn.error")


class ChatInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000, pattern=r"\S")


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat")
async def chat(body: ChatInput, request: Request):
    async def generate():
        yield sse("start", {"model": "fake-ai"})
        chunks = stream_reply(body.prompt)

        try:
            async for text in chunks:
                if await request.is_disconnected():
                    return

                yield sse("delta", {"text": text})

            yield sse("done", {"reason": "stop"})
        except Exception:
            logger.exception("模型生成失败")
            # 响应头已经发送，流中错误通过事件传递。
            yield sse("error", {"message": "模型生成失败，请重试。"})
        finally:
            # 关闭本次请求持有的模型流；取消不会被上面的 Exception 捕获。
            await chunks.aclose()
            logger.info("本次生成结束或客户端断开")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
