"""运行：python -m uvicorn main:app --reload --port 8003。"""

import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from model import MODEL, stream_reply

app = FastAPI(title="SSE DeepSeek Multi-Turn Demo")
logger = logging.getLogger("uvicorn.error")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000, pattern=r"\S")


class ChatInput(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=41)

    @model_validator(mode="after")
    def validate_history(self):
        for index, message in enumerate(self.messages):
            expected = "user" if index % 2 == 0 else "assistant"
            if message.role != expected:
                raise ValueError("消息必须从 user 开始，按 user / assistant 交替")
        if self.messages[-1].role != "user":
            raise ValueError("最后一条消息必须是 user")
        if len(self.messages[-1].content) > 2000:
            raise ValueError("本轮问题最多 2000 字符")
        if sum(len(message.content) for message in self.messages) > 60000:
            raise ValueError("对话内容过长，请开始新对话")
        return self


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat")
async def chat(body: ChatInput, request: Request):
    async def generate():
        yield sse("start", {"model": MODEL})
        chunks = stream_reply([message.model_dump() for message in body.messages])

        try:
            async for text in chunks:
                if await request.is_disconnected():
                    return

                yield sse("delta", {"text": text})

            yield sse("done", {"reason": "stop"})
        except Exception as exc:
            logger.error("模型生成失败，错误类型：%s", type(exc).__name__)
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

