"""运行：python -m uvicorn main:app --reload --port 8012。"""

import json
import logging
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, SchemaError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from model import StructuredOutputError, complete_reply, stream_reply
from anthropic import APITimeoutError as AnthropicTimeoutError
from openai import APITimeoutError as OpenAITimeoutError
from output_monitor import OutputAnomalyError

app = FastAPI(title="Streaming and Non-streaming Demo")
logger = logging.getLogger("uvicorn.error")


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12000, pattern=r"\S")


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["json_object", "json_schema"]
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    json_schema: dict[str, Any] | StrictBool | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def validate_format(self):
        if self.type == "json_object":
            if self.model_fields_set != {"type"}:
                raise ValueError("json_object 仅接受 type 字段")
        else:
            if self.name is None or self.json_schema is None:
                raise ValueError("json_schema 必须提供 name 和 schema")
            try:
                Draft202012Validator.check_schema(self.json_schema)
            except SchemaError as exc:
                raise ValueError("无效的 JSON Schema：" + exc.message) from exc
        return self


class ChatInput(BaseModel):
    response_format: ResponseFormat | None = None
    stream: bool = True
    model: Literal["deepseek-flash", "deepseek-v4-pro"] = "deepseek-flash"
    prompt: str | None = Field(default=None, min_length=1, max_length=2000, pattern=r"\S")
    messages: list[Message] = Field(default_factory=list, max_length=41)

    @model_validator(mode="after")
    def validate_history(self):
        if self.prompt is not None:
            if "messages" in self.model_fields_set:
                raise ValueError("prompt 和 messages 不能同时传入")
            self.messages = [Message(role="user", content=self.prompt)]
        if not self.messages:
            raise ValueError("必须提供非空 prompt 或 messages")
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
    if not body.stream:
        try:
            options = {}
            if body.response_format is not None:
                options["response_format"] = body.response_format.model_dump(by_alias=True, exclude_none=True)
            return await complete_reply(
                [message.model_dump() for message in body.messages], model=body.model, **options
            )
        except StructuredOutputError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except (OpenAITimeoutError, AnthropicTimeoutError) as exc:
            logger.error("模型调用超时，错误类型：%s", type(exc).__name__)
            raise HTTPException(status_code=504, detail="模型调用超时，请重试。") from exc
        except Exception as exc:
            logger.error("模型生成失败，错误类型：%s", type(exc).__name__)
            raise HTTPException(status_code=502, detail="模型生成失败，请重试。") from exc

    async def generate():
        yield sse("start", {"model": body.model})
        options = {}
        if body.response_format is not None:
            options["response_format"] = body.response_format.model_dump(by_alias=True, exclude_none=True)
        chunks = stream_reply(
            [message.model_dump() for message in body.messages], model=body.model, **options
        )

        try:
            async for event in chunks:
                if await request.is_disconnected():
                    return

                if event.type == "delta":
                    yield sse("delta", {"text": event.text})
                elif event.type == "done":
                    yield sse("done", {"reason": event.finish_reason})
                elif event.type == "usage":
                    yield sse("usage", event.usage)
        except (OutputAnomalyError, StructuredOutputError) as exc:
            yield sse("error", {"message": str(exc)})
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

