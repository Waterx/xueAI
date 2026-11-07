"""运行：python -m uvicorn main:app --reload --port 8012。"""

import json
import logging
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator, SchemaError

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, model_validator

from model import StructuredOutputError, complete_reply, stream_reply
from prompt_manager import render_prompt, save_template
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
    template_id: str | None = Field(default=None, min_length=1, pattern=r"\S")
    template_version: int | None = Field(default=None, ge=1, strict=True)
    variables: dict[str, str] = Field(default_factory=dict)
    messages: list[Message] | None = Field(default=None, max_length=41)
    _model_messages: list[Message] = PrivateAttr()

    @model_validator(mode="after")
    def validate_history(self):
        if self.template_version is not None and self.template_id is None:
            raise ValueError("仅在提供 template_id 时允许设置 template_version")
        sources = sum((self.prompt is not None, self.template_id is not None,
                       self.messages is not None))
        if sources != 1:
            raise ValueError("必须且只能提供 prompt、template_id 或 messages 其中一种")
        messages = self._resolve_messages()
        for index, message in enumerate(messages):
            expected = "user" if index % 2 == 0 else "assistant"
            if message.role != expected:
                raise ValueError("消息必须从 user 开始，按 user / assistant 交替")
        if messages[-1].role != "user":
            raise ValueError("最后一条消息必须是 user")
        if len(messages[-1].content) > 2000:
            raise ValueError("本轮问题最多 2000 字符")
        if sum(len(message.content) for message in messages) > 60000:
            raise ValueError("对话内容过长，请开始新对话")
        self._model_messages = messages
        return self

    def _resolve_messages(self) -> list[Message]:
        if self.template_id is not None:
            prompt = render_prompt(self.template_id, self.variables, self.template_version)
            if not prompt.strip() or len(prompt) > 2000:
                raise ValueError("渲染后的 prompt 必须非空且最多 2000 字符")
            return [Message(role="user", content=prompt)]
        if self.prompt is not None:
            return [Message(role="user", content=self.prompt)]
        if not self.messages:
            raise ValueError("必须提供非空 prompt、template_id 或 messages")
        return self.messages

    def model_messages(self) -> list[dict[str, str]]:
        return [message.model_dump() for message in self._model_messages]


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "index.html")


class TemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str = Field(min_length=1, pattern=r"\S")
    content: str = Field(min_length=1, pattern=r"\S")


@app.post("/templates")
def create_template(body: TemplateInput):
    version = save_template(body.template_id, body.content)
    return {"template_id": body.template_id, "version": version}


@app.post("/chat")
async def chat(body: ChatInput, request: Request):
    messages = body.model_messages()
    if not body.stream:
        try:
            options = {}
            if body.response_format is not None:
                options["response_format"] = body.response_format.model_dump(by_alias=True, exclude_none=True)
            return await complete_reply(messages, model=body.model, **options)
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
        chunks = stream_reply(messages, model=body.model, **options)

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

