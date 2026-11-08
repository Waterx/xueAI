from rate_limiter import RateLimiter
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import httpx
from starlette.requests import ClientDisconnect

from main import ChatInput, app, chat
from metrics import MetricsStore
from schemas import LLMEvent, LLMResult


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("main.rate_limiter", RateLimiter({"deepseek-flash": 10000, "deepseek-v4-pro": 10000})))
        directory = self.enterContext(TemporaryDirectory())
        self.db_path = Path(directory) / "metrics.db"
        self.store = MetricsStore(self.db_path)
        self.enterContext(patch("main.metrics_store", self.store))
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ))

    async def test_non_streaming_totals_are_separated_by_model(self):
        async def complete(messages, model):
            return LLMResult(
                model=model, content="hello", finish_reason="stop",
                usage={"prompt_tokens": 5, "total_tokens": 8},
            )

        self.assertEqual((await self.client.get("/metrics")).json(), {})
        with patch("main.complete_reply", complete), patch(
            "main.perf_counter", side_effect=[0, 0.125, 1, 1.25, 2, 2.5]
        ):
            for model in ["deepseek-flash", "deepseek-v4-pro", "deepseek-flash"]:
                response = await self.client.post(
                    "/chat", json={"model": model, "prompt": "hi", "stream": False},
                )
                self.assertEqual(response.status_code, 200)

        metrics = (await self.client.get("/metrics")).json()
        self.assertEqual(metrics["deepseek-flash"], {
            "total_requests": 2, "success_requests": 2, "error_requests": 0,
            "cancelled_requests": 0, "input_tokens": 10, "output_tokens": 0,
            "total_tokens": 16, "avg_latency_ms": 312.5, "avg_ttft_ms": None,
        })
        self.assertEqual(metrics["deepseek-v4-pro"]["total_requests"], 1)
        self.assertEqual(metrics["deepseek-v4-pro"]["avg_latency_ms"], 250)

    async def test_first_nonempty_delta_and_usage_after_done(self):
        async def stream(messages, model, **options):
            for text in ("", "{", "}"):
                yield LLMEvent(type="delta", text=text)
            yield LLMEvent(type="usage", usage={"prompt_tokens": 10, "cache_read_input_tokens": 4})
            yield LLMEvent(type="done", finish_reason="stop")
            yield LLMEvent(type="usage", usage={"completion_tokens": 2, "reasoning_tokens": 0})

        adapter = Mock(stream=stream)
        with patch("model.get_adapter", return_value=adapter), patch(
            "main.perf_counter", side_effect=[0, 0.125, 0.5]
        ):
            response = await self.client.post("/chat", json={
                "prompt": "hi", "response_format": {"type": "json_object"},
            })
        self.assertLess(response.text.rindex("event: usage"), response.text.index("event: done"))
        stats = self.store.snapshot()["deepseek-flash"]
        self.assertEqual(stats["success_requests"], 1)
        self.assertEqual(stats["avg_ttft_ms"], 125)
        self.assertEqual(stats["avg_latency_ms"], 500)
        self.assertEqual(stats["output_tokens"], 2)
        row, = self.store.recent(10, None)
        self.assertEqual(row["request_id"], response.headers["X-Request-ID"])
        self.assertEqual(row["conversation_id"], response.headers["X-Conversation-ID"])
        self.assertEqual(row["usage"], {
            "prompt_tokens": 10, "cache_read_input_tokens": 4,
            "completion_tokens": 2, "reasoning_tokens": 0,
        })

    async def test_no_text_or_done_does_not_count_as_success(self):
        async def stream(messages, model):
            yield LLMEvent(type="delta", text="")

        with patch("main.stream_reply", stream):
            response = await self.client.post("/chat", json={"prompt": "hi"})
        self.assertNotIn("event: done", response.text)
        stats = self.store.snapshot()["deepseek-flash"]
        self.assertEqual(stats["error_requests"], 1)
        self.assertIsNone(stats["avg_ttft_ms"])

    async def test_invalid_input_and_model_errors(self):
        invalid = await self.client.post("/chat", json={})
        self.assertEqual(invalid.status_code, 422)
        UUID(invalid.headers["X-Request-ID"])
        self.assertEqual(self.store.snapshot(), {})

        with patch("main.complete_reply", side_effect=RuntimeError):
            response = await self.client.post("/chat", json={"prompt": "hi", "stream": False})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "INTERNAL_ERROR")
        stats = self.store.snapshot()["deepseek-flash"]
        self.assertEqual(stats["error_requests"], 1)
        self.assertGreaterEqual(stats["avg_latency_ms"], 0)

    async def test_http_and_sse_errors_match_sqlite_request_ids(self):
        async def stream(messages, model):
            yield LLMEvent(type="delta", text="部分内容")
            raise httpx.ReadTimeout("private upstream detail")

        body = {"prompt": "hi", "model": "deepseek-v4-pro"}
        with patch("main.complete_reply", side_effect=httpx.ReadTimeout("private upstream detail")):
            complete = await self.client.post("/chat", json={**body, "stream": False})
        with patch("main.stream_reply", stream):
            streamed = await self.client.post("/chat", json=body)

        self.assertEqual(complete.status_code, 504)
        self.assertEqual(streamed.status_code, 200)
        self.assertIn("event: delta", streamed.text)
        self.assertNotIn("event: done", streamed.text)
        http_error = complete.json()["error"]
        sse_error = json.loads(streamed.text.split("event: error\ndata: ")[1])
        for response, payload in ((complete, http_error), (streamed, sse_error)):
            request_id = response.headers["X-Request-ID"]
            self.assertEqual(payload.pop("request_id"), request_id)
            self.assertNotIn("private upstream detail", response.text)
            result = await self.client.get("/metrics/requests", params={"request_id": request_id})
            row, = result.json()
            self.assertEqual(row["request_id"], request_id)
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["error_code"], payload["code"])
            self.assertEqual(row["error_message"], payload["message"])
            self.assertNotIn("private upstream detail", result.text)
        self.assertEqual(http_error, sse_error)
        self.assertEqual(http_error["code"], "UPSTREAM_TIMEOUT")
        self.assertIs(http_error["retryable"], True)

    async def test_template_database_failure_is_internal_without_model_metrics(self):
        with patch("main.render_prompt", side_effect=sqlite3.OperationalError("private database path")):
            response = await self.client.post("/chat", json={"template_id": "translate"})
        self.assertEqual(response.status_code, 500)
        error = response.json()["error"]
        self.assertEqual(error["code"], "INTERNAL_ERROR")
        self.assertEqual(error["request_id"], response.headers["X-Request-ID"])
        self.assertNotIn("private database path", response.text)
        self.assertEqual(self.store.snapshot(), {})

    async def test_request_history_and_totals_survive_reopening_database(self):
        result = LLMResult(
            model="deepseek-flash", content="hello", finish_reason="stop",
            usage={"completion_tokens": 0},
        )
        body = {"prompt": "hi", "stream": False}
        with patch("main.complete_reply", side_effect=[result, RuntimeError()]):
            self.assertEqual((await self.client.post("/chat", json=body)).status_code, 200)
            self.assertEqual((await self.client.post("/chat", json=body)).status_code, 500)

        with patch("main.metrics_store", MetricsStore(self.db_path)):
            latest = (await self.client.get("/metrics/requests?limit=1")).json()
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]["status"], "error")
            self.assertIsNone(latest[0]["output_tokens"])
            self.assertIsNone(latest[0]["usage"])
            older = await self.client.get("/metrics/requests", params={
                "limit": 1, "before_id": latest[0]["id"],
            })
            row, = older.json()
            self.assertEqual(row["status"], "success")
            self.assertIs(row["stream"], False)
            self.assertIsNone(row["ttft_ms"])
            self.assertIsNone(row["input_tokens"])
            self.assertEqual(row["output_tokens"], 0)
            self.assertEqual(row["usage"], {"completion_tokens": 0})
            self.assertTrue(row["completed_at"].endswith("Z"))
            totals = (await self.client.get("/metrics")).json()["deepseek-flash"]
            self.assertEqual(totals["total_requests"], 2)

    async def test_observe_by_request_and_conversation(self):
        result = LLMResult(model="deepseek-flash", content="hello", finish_reason="stop")
        body = {"prompt": "hi", "stream": False}
        with patch("main.complete_reply", return_value=result):
            first = await self.client.post("/chat", json=body)
            conversation_id = first.headers["X-Conversation-ID"]
            second = await self.client.post("/chat", json={**body, "conversation_id": conversation_id})
            other = await self.client.post("/chat", json=body)

        self.assertEqual(second.headers["X-Conversation-ID"], conversation_id)
        self.assertNotEqual(other.headers["X-Conversation-ID"], conversation_id)
        ids = [response.headers["X-Request-ID"] for response in (first, second, other)]
        self.assertEqual(len(set(ids)), 3)

        filters = {"conversation_id": conversation_id}
        grouped = (await self.client.get("/metrics/requests", params=filters)).json()
        self.assertEqual([row["request_id"] for row in grouped], ids[:2][::-1])
        summary = (await self.client.get("/metrics", params=filters)).json()
        self.assertEqual(summary["deepseek-flash"]["total_requests"], 2)
        match = (await self.client.get("/metrics/requests", params={"request_id": ids[1]})).json()
        self.assertEqual(match, grouped[:1])
        mismatch = await self.client.get("/metrics/requests", params={**filters, "request_id": ids[2]})
        self.assertEqual(mismatch.json(), [])

    async def test_schema_upgrade_keeps_older_rows_without_ids(self):
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.executescript("""
                DROP INDEX request_metrics_request_id;
                DROP INDEX request_metrics_conversation_id;
                ALTER TABLE request_metrics DROP COLUMN request_id;
                ALTER TABLE request_metrics DROP COLUMN conversation_id;
                ALTER TABLE request_metrics DROP COLUMN usage_json;
                ALTER TABLE request_metrics DROP COLUMN error_code;
                ALTER TABLE request_metrics DROP COLUMN error_message;
                INSERT INTO request_metrics (model, stream, status, latency_ms, input_tokens)
                VALUES ('deepseek-flash', 0, 'success', 100, 3);
            """)
        upgraded = MetricsStore(self.db_path)
        row, = upgraded.recent(10, None)
        self.assertIsNone(row["request_id"])
        self.assertIsNone(row["conversation_id"])
        self.assertIsNone(row["usage"])
        self.assertIsNone(row["error_code"])
        self.assertIsNone(row["error_message"])
        self.assertEqual(upgraded.snapshot()["deepseek-flash"]["input_tokens"], 3)

    async def test_structured_validation_errors_keep_reported_usage(self):
        usage = {"prompt_tokens": 4, "completion_tokens": 1}
        result = LLMResult(
            model="deepseek-flash", content="invalid", finish_reason="stop", usage=usage,
        )

        async def stream(messages, model, **options):
            yield LLMEvent(type="delta", text=result.content)
            yield LLMEvent(type="done", finish_reason="stop")
            yield LLMEvent(type="usage", usage=usage)

        adapter = Mock(complete=AsyncMock(return_value=result), stream=stream)
        body = {"prompt": "hi", "response_format": {"type": "json_object"}}
        with patch("model.get_adapter", return_value=adapter):
            complete = await self.client.post("/chat", json={**body, "stream": False})
            streamed = await self.client.post("/chat", json=body)
        self.assertEqual(complete.status_code, 502)
        self.assertIn("event: error", streamed.text)
        self.assertNotIn("event: done", streamed.text)
        stats = self.store.snapshot()["deepseek-flash"]
        self.assertEqual(stats["error_requests"], 2)
        self.assertEqual(stats["success_requests"], 0)
        self.assertEqual(stats["input_tokens"], 8)
        self.assertEqual(stats["output_tokens"], 2)

    async def test_disconnect_during_sse_send_closes_and_records_once(self):
        closed = asyncio.Event()

        async def stream(messages, model):
            try:
                yield LLMEvent(type="delta", text="first")
                self.fail("stream resumed after client disconnected")
            finally:
                closed.set()

        async def send(message):
            if message.get("body", b"").startswith(b"event: delta"):
                raise OSError("client disconnected")

        request = Mock(is_disconnected=AsyncMock(return_value=False))
        request.state.request_id = str(uuid4())
        with patch("main.stream_reply", stream):
            response = await chat(ChatInput(prompt="hi"), request)
            scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
            with self.assertRaises(ClientDisconnect):
                await response(scope, AsyncMock(), send)
        self.assertTrue(closed.is_set())
        stats = self.store.snapshot()["deepseek-flash"]
        self.assertEqual(stats["total_requests"], 1)
        self.assertEqual(stats["cancelled_requests"], 1)
        self.assertEqual(stats["success_requests"], 0)
        row, = self.store.recent(10, None)
        self.assertEqual(row["status"], "cancelled")
        self.assertIs(row["stream"], True)

    async def test_disconnect_while_waiting_for_provider(self):
        waiting = asyncio.Event()
        closed = asyncio.Event()

        async def stream(messages, model):
            try:
                waiting.set()
                await asyncio.Future()
                yield LLMEvent(type="done", finish_reason="stop")
            finally:
                closed.set()

        async def receive():
            await waiting.wait()
            return {"type": "http.disconnect"}

        request = Mock(is_disconnected=AsyncMock(return_value=False))
        request.state.request_id = str(uuid4())
        send = AsyncMock()
        with patch("main.stream_reply", stream):
            response = await chat(ChatInput(prompt="hi"), request)
            scope = {"type": "http", "asgi": {"spec_version": "2.3"}}
            await asyncio.wait_for(response(scope, receive, send), timeout=1)

        self.assertTrue(closed.is_set())
        for call in send.await_args_list:
            self.assertNotIn(b"event: error", call.args[0].get("body", b""))
        stats = self.store.snapshot()["deepseek-flash"]
        self.assertEqual(stats["total_requests"], 1)
        self.assertEqual(stats["cancelled_requests"], 1)
        self.assertIsNone(stats["avg_ttft_ms"])
