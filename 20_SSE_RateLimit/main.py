"""运行：python -m uvicorn main:app --reload --port 8013。"""

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import UUID, uuid4

from anyio import CancelScope
from jsonschema import Draft202012Validator, SchemaError

from fastapi import FastAPI, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message as ASGIMessage, Receive, Scope, Send

from errors import RateLimitError, GatewayError, StructuredOutputError, UpstreamResponseError, normalize_error
from metrics import MetricsStore, RequestMetrics, RequestStatus
from model import complete_reply, stream_reply
from prompt_manager import render_prompt, save_template
from retry import with_retry, stream_with_retry
from rate_limiter import RateLimiter

app = FastAPI(title="Streaming and Non-streaming Demo")
logger = logging.getLogger("uvicorn.error")
metrics_store = MetricsStore()
rate_limiter = RateLimiter()


def report_error(exc: Exception, request_id: str) -> GatewayError:
    error = normalize_error(exc)
    logger.error("请求失败 request_id=%s code=%s exception=%s",
                 request_id, error.code, type(exc).__name__)
    return error


def error_response(
    error: GatewayError, request_id: str, headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse({"error": error.payload(request_id)},
                        status_code=error.status_code, headers=headers)


class ChatRequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != "/chat":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        request_id = state["request_id"] = str(uuid4())
        started = False

        async def send_with_identity(message: ASGIMessage) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if "conversation_id" in state:
                    headers["X-Conversation-ID"] = state["conversation_id"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_identity)
        except ClientDisconnect:
            raise
        except Exception as exc:
            if started:
                raise
            response = error_response(report_error(exc, request_id), request_id)
            await response(scope, receive, send_with_identity)


app.add_middleware(ChatRequestMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", None)
    if request_id is None:
        return await request_validation_exception_handler(request, exc)
    return error_response(normalize_error(exc), request_id)


class ChatStreamingResponse(StreamingResponse):
    async def stream_response(self, send: Send) -> None:
        try:
            await super().stream_response(send)
        finally:
            # 发送 SSE 时也可能断开，此时生成器正暂停在 yield。
            with CancelScope(shield=True):
                await self.body_iterator.aclose()


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
    conversation_id: UUID | None = None
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


@app.get("/observability", include_in_schema=False)
async def observability():
    return FileResponse(Path(__file__).parent / "observability.html")


class TemplateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str = Field(min_length=1, pattern=r"\S")
    content: str = Field(min_length=1, pattern=r"\S")


@app.post("/templates")
def create_template(body: TemplateInput):
    version = save_template(body.template_id, body.content)
    return {"template_id": body.template_id, "version": version}


@app.get("/metrics")
def get_metrics(request_id: UUID | None = None, conversation_id: UUID | None = None):
    return metrics_store.snapshot(
        str(request_id) if request_id else None, str(conversation_id) if conversation_id else None,
    )


@app.get("/metrics/requests")
def get_request_metrics(
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
    request_id: UUID | None = None,
    conversation_id: UUID | None = None,
):
    return metrics_store.recent(
        limit, before_id,
        str(request_id) if request_id else None, str(conversation_id) if conversation_id else None,
    )


async def record_metrics(metrics: RequestMetrics) -> None:
    with CancelScope(shield=True):
        try:
            await run_in_threadpool(metrics_store.record, metrics)
        except sqlite3.Error:
            logger.exception("保存模型调用指标失败 request_id=%s", metrics.request_id)
    logger.info(
        "模型调用结束 request_id=%s conversation_id=%s model=%s status=%s latency_ms=%.1f",
        metrics.request_id, metrics.conversation_id, metrics.model, metrics.status, metrics.latency_ms,
    )


@app.post("/chat")
async def chat(body: ChatInput, request: Request):
    request_id = request.state.request_id
    conversation_id = str(body.conversation_id or uuid4())
    request.state.conversation_id = conversation_id
    messages = body.model_messages()
    options = {}
    if body.response_format is not None:
        options["response_format"] = body.response_format.model_dump(by_alias=True, exclude_none=True)

    try:
        await rate_limiter.acquire(body.model)
    except RateLimitError as exc:
        return error_response(exc, request_id, {"Retry-After": str(exc.retry_after)})

    if not body.stream:
        started = perf_counter()
        status: RequestStatus = "error"
        usage = None
        error = None
        try:
            result = await with_retry(
                lambda: complete_reply(messages, model=body.model, **options)
            )
            usage = result.usage
            status = "success"
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            if isinstance(exc, StructuredOutputError):
                usage = exc.usage
            error = report_error(exc, request_id)
            return error_response(error, request_id)
        finally:
            await record_metrics(RequestMetrics(
                model=body.model, stream=False, status=status, usage=usage,
                latency_ms=(perf_counter() - started) * 1000,
                request_id=request_id, conversation_id=conversation_id,
                error_code=error.code if status == "error" and error else None,
                error_message=error.message if status == "error" and error else None,
            ))

    async def generate():
        started = perf_counter()
        status: RequestStatus = "error"
        usage: dict[str, int] | None = None
        ttft_ms = None
        saw_done = False
        done_reason = None
        error = None
        chunks = None

        try:
            yield sse("start", {"model": body.model})
            chunks = stream_with_retry(
                lambda: stream_reply(messages, model=body.model, **options)
            )
            async for event in chunks:
                if event.type == "delta" and event.text and ttft_ms is None:
                    ttft_ms = (perf_counter() - started) * 1000
                if event.type == "usage":
                    usage = (usage or {}) | event.usage
                if await request.is_disconnected():
                    status = "cancelled"
                    return

                if event.type == "delta":
                    yield sse("delta", {"text": event.text})
                elif event.type == "done":
                    saw_done = True
                    done_reason = event.finish_reason
                elif event.type == "usage":
                    yield sse("usage", event.usage)
            if not saw_done:
                raise UpstreamResponseError("模型流意外结束，缺少 done 事件")
            status = "success"
        except (asyncio.CancelledError, GeneratorExit):
            status = "cancelled"
            raise
        except Exception as exc:
            error = report_error(exc, request_id)
        finally:
            try:
                if chunks is not None:
                    with CancelScope(shield=True):
                        await chunks.aclose()
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                cleanup_error = report_error(exc, request_id)
                if status != "cancelled" and error is None:
                    status = "error"
                    error = cleanup_error
            finally:
                await record_metrics(RequestMetrics(
                    model=body.model, stream=True, status=status, usage=usage,
                    ttft_ms=ttft_ms, latency_ms=(perf_counter() - started) * 1000,
                    request_id=request_id, conversation_id=conversation_id,
                    error_code=error.code if status == "error" and error else None,
                    error_message=error.message if status == "error" and error else None,
                ))

        if error is not None:
            yield sse("error", error.payload(request_id))
        elif status == "success":
            yield sse("done", {"reason": done_reason})

    return ChatStreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

