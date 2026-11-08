"""使用真实 SDK 和模拟 HTTP 流验证上游资源生命周期。"""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from adapters.openai_adapter import OpenAIAdapter
from model import stream_reply
from output_monitor import OutputAnomalyError


class TrackedStream(httpx.AsyncByteStream):
    """模拟上游响应体：可正常结束、读取失败，或阻塞等待取消。"""

    def __init__(self, texts, *, fail=False, block=False):
        self.texts = texts
        self.fail = fail
        self.block = block
        self.closed = False
        self.waiting = asyncio.Event()

    def chunk(self, delta, reason=None):
        # 构造 OpenAI 兼容的 SSE 字节，让真实 SDK 执行解析与事件转换。
        payload = {
            "id": "resource-test", "object": "chat.completion.chunk",
            "created": 0, "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    async def __aiter__(self):
        for text in self.texts:
            yield self.chunk({"content": text})
        if self.fail:
            # 已发送部分文字后模拟读取失败，验证流中途出错的清理路径。
            raise httpx.ReadError("simulated upstream failure")
        if self.block:
            # 通知测试：消费任务已进入上游等待，此时取消可命中读取阶段。
            self.waiting.set()
            await asyncio.Event().wait()
        yield self.chunk({}, "stop")
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        # 由 HTTP 响应关闭流程调用，供测试观察上游响应体是否已释放。
        self.closed = True


class AdapterResourceTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, source, wire, mode):
        """对指定模型入口执行一种场景，并验证事件结果与资源释放。"""

        def handle(request):
            return httpx.Response(
                200, stream=wire, headers={"content-type": "text/event-stream"}
            )

        # 仅替换网络传输；HTTP 客户端、SDK 和 Adapter 均执行真实逻辑。
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        client = AsyncOpenAI(api_key="test", http_client=http_client)
        received = []
        # 使用假密钥和本地响应，不请求真实 API；屏蔽异常监测的文件日志。
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test"}), patch(
            "adapters.openai_adapter.AsyncOpenAI", return_value=client
        ), patch("output_monitor.logger.error") as anomaly_log:
            events = source([{"role": "user", "content": "你好"}], "deepseek-flash")

            async def consume():
                async for event in events:
                    received.append(event)

            try:
                if mode == "cancel":
                    task = asyncio.create_task(consume())
                    try:
                        # 用事件同步而非 sleep，确保上游正在等待后再取消。
                        await asyncio.wait_for(wire.waiting.wait(), timeout=2)
                        task.cancel()
                        # 取消必须向调用方传播，不能被吞掉或当作正常结束。
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                elif mode == "close":
                    # 模拟消费方读到一段文字后主动关闭生成器。
                    received.append(await anext(events))
                    await events.aclose()
                elif mode == "failure":
                    with self.assertRaises(httpx.ReadError):
                        await consume()
                elif mode == "anomaly":
                    # 标记跨两个文本增量，验证 OutputMonitor 仍在发出前检查。
                    with self.assertRaises(OutputAnomalyError):
                        await consume()
                    anomaly_log.assert_called_once()
                else:
                    await consume()

                # 在清理测试自身之前断言，避免测试掩盖生产代码的资源泄漏。
                self.assertTrue(wire.closed)
                self.assertTrue(http_client.is_closed)
                self.assertTrue(client.is_closed())
                if mode == "normal":
                    self.assertEqual([event.type for event in received], ["delta", "done"])
                else:
                    self.assertNotIn("done", [event.type for event in received])
                if mode == "anomaly":
                    # 首段已发出；拼成异常标记的第二段必须被拦截。
                    self.assertEqual([event.text for event in received], ["你好<|im_"])
            finally:
                # 仅为测试失败时兜底清理，不参与上面的资源关闭验收。
                await events.aclose()
                await client.close()

    async def check_sources(self, mode, texts, **kwargs):
        # 同时覆盖直接使用 Adapter，以及 HTTP 层沿用的 model.py 兼容入口。
        for source in (OpenAIAdapter().stream, stream_reply):
            with self.subTest(source=source.__qualname__):
                await self.exercise(source, TrackedStream(texts, **kwargs), mode)

    async def test_normal_completion_closes_resources(self):
        await self.check_sources("normal", ["你好"])

    async def test_upstream_failure_closes_resources(self):
        await self.check_sources("failure", ["你好"], fail=True)

    async def test_cross_chunk_output_anomaly_closes_resources(self):
        await self.check_sources("anomaly", ["你好<|im_", "end|>"])

    async def test_client_cancellation_closes_resources(self):
        await self.check_sources("cancel", ["你好"], block=True)

    async def test_explicit_close_between_deltas_closes_resources(self):
        await self.check_sources("close", ["你好"], block=True)
