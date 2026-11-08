#!/usr/bin/env python3
"""复用完整 unittest 的离线验收；--live 另发出四次真实模型请求。"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from importlib.metadata import version
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GROUPS = {
    "Model routing and contracts": {"test_registry", "test_adapter", "test_model"},
    "Non-streaming and unified errors": {"test_complete"},
    "SSE and protocol resource cleanup": {"test_anthropic_adapter", "test_responses_adapter", "test_chat"},
    "JSON Object": {"test_json_object"},
    "JSON Schema": {"test_json_schema"},
    "Streaming JSON and cancellation": {"test_streaming_json"},
    "Prompt template rendering": {"test_prompt_manager"},
    "Prompt version lookup": {"test_template_versions"},
    "Token usage and cache accounting": {"test_token_usage"},
    "Metrics, latency, TTFT and disconnect": {"test_metrics"},
    "Exponential backoff retry": {"test_retry"},
    "Per-model rate limiting": {"test_rate_limit"},
    "Output anomaly monitoring": {"test_output_monitor"},
    "Legacy adapter regression": {"test_openai_adapter", "test_openai_model", "test_adapter_resources"},
}


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def startTest(self, test):
        self.outcomes[test.id()] = True
        super().startTest(test)

    def addFailure(self, test, err):
        self.outcomes[test.id()] = False
        super().addFailure(test, err)

    def addError(self, test, err):
        self.outcomes[test.id()] = False
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.outcomes[test.id()] = False
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.outcomes[test.id()] = False
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.outcomes[test.id()] = False
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.outcomes[test.id()] = False
        super().addSubTest(test, subtest, err)


def offline():
    import main
    import prompt_manager
    from metrics import MetricsStore

    transcript = io.StringIO()
    log_handler = logging.StreamHandler(transcript)
    logger = logging.getLogger("uvicorn.error")
    original_handlers, original_propagate = logger.handlers[:], logger.propagate
    connect = socket.socket.connect

    def no_network(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("Offline verification forbids real network connections")
        return connect(sock, address)

    try:
        logger.handlers = [log_handler]
        logger.propagate = False
        with tempfile.TemporaryDirectory(prefix="gateway-verify-") as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(main, "metrics_store", MetricsStore(Path(tmp) / "metrics.db")))
            stack.enter_context(patch.object(prompt_manager, "DB_PATH", Path(tmp) / "prompts.db"))
            stack.enter_context(patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline-test-only"}))
            stack.enter_context(patch.object(socket.socket, "connect", no_network))
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
            result = unittest.TextTestRunner(stream=transcript, verbosity=2, resultclass=Result).run(suite)
    finally:
        logger.handlers = original_handlers
        logger.propagate = original_propagate

    groups = []
    for label, modules in GROUPS.items():
        outcomes = [ok for name, ok in result.outcomes.items() if name.split(".")[0] in modules]
        ok = bool(outcomes) and all(outcomes)
        groups.append({"name": label, "passed": ok, "tests": len(outcomes)})
        print(f"[{'PASS' if ok else 'FAIL'}] {label} ({len(outcomes)} tests)", flush=True)
    passed = sum(result.outcomes.values())
    failed = len(result.outcomes) - passed
    ok = result.wasSuccessful() and failed == 0 and all(g["passed"] for g in groups)
    print(f"\n{passed} tests passed, {failed} failed; {sum(g['passed'] for g in groups)}/{len(groups)} groups passed")
    if not ok:
        print(transcript.getvalue())
    return {"passed": ok, "tests_run": result.testsRun, "tests_passed": passed,
            "tests_failed_or_skipped": failed, "groups": groups}


async def live():
    import httpx
    import main
    from metrics import MetricsStore
    from rate_limiter import RateLimiter

    checks = []
    schema = {"type": "object", "properties": {"ok": {"const": True}},
              "required": ["ok"], "additionalProperties": False}
    with tempfile.TemporaryDirectory(prefix="gateway-live-") as tmp, patch.object(
        main, "metrics_store", MetricsStore(Path(tmp) / "metrics.db")
    ), patch.object(main, "rate_limiter", RateLimiter()):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://gateway") as client:
            for model in ("deepseek-flash", "deepseek-v4-pro"):
                for streaming in (False, True):
                    check = {"model": model, "stream": streaming, "passed": False}
                    body = {"model": model, "stream": streaming, "prompt": "Reply with exactly OK."}
                    if streaming:
                        body.update(prompt='Return exactly this JSON object: {"ok":true}',
                                    response_format={"type": "json_schema", "name": "smoke", "schema": schema})
                    try:
                        response = await asyncio.wait_for(client.post("/chat", json=body), timeout=90)
                        check["http_status"] = response.status_code
                        check["request_id"] = response.headers.get("x-request-id")
                        if response.status_code != 200:
                            check["error_code"] = response.json().get("error", {}).get("code")
                            raise ValueError("Non-200 HTTP response")
                        if streaming:
                            frames = [frame.split("\ndata: ", 1) for frame in response.text.strip().split("\n\n")]
                            events = [(name.removeprefix("event: "), json.loads(data)) for name, data in frames]
                            check["events"] = [name for name, data in events]
                            if any(name == "error" for name, data in events):
                                check["error_code"] = next(data["code"] for name, data in events if name == "error")
                            if events[0][0] != "start" or events[-1][0] != "done" or "error" in check["events"]:
                                raise ValueError("Invalid SSE termination")
                            content = "".join(data["text"] for name, data in events if name == "delta")
                            if json.loads(content) != {"ok": True}:
                                raise ValueError("Unexpected structured content")
                        else:
                            payload = response.json()
                            if payload["model"] != model or payload["finish_reason"] != "stop" or not payload["content"].strip():
                                raise ValueError("Invalid completion")
                            content = payload["content"]
                        rows = (await client.get("/metrics/requests", params={"request_id": check["request_id"]})).json()
                        if len(rows) != 1 or rows[0]["status"] != "success":
                            raise ValueError("Missing success metric")
                        row = rows[0]
                        usage = row["usage"]
                        if not usage or any(type(usage.get(k)) is not int for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
                            raise ValueError("Missing token totals")
                        if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
                            raise ValueError("Inconsistent token totals")
                        if streaming and row["ttft_ms"] is None:
                            raise ValueError("Missing TTFT")
                        check.update(passed=True, content=content, usage=usage,
                                     latency_ms=row["latency_ms"], ttft_ms=row["ttft_ms"])
                    except Exception as exc:
                        # Do not print provider response bodies, credentials or arbitrary exception text.
                        check["failure_type"] = type(exc).__name__
                    checks.append(check)
                    print(f"[{'PASS' if check['passed'] else 'FAIL'}] LIVE {model} {'SSE JSON Schema' if streaming else 'non-streaming'}", flush=True)
    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "transport": "In-process ASGI gateway with real provider HTTPS; not a TCP streaming benchmark"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also run four real, billable model requests")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="only read with --live")
    parser.add_argument("--report", type=Path, help="write a JSON report (no API key)")
    args = parser.parse_args()
    print("LLM Gateway Verification\n========================\nMode: offline MockTransport (no API tokens)", flush=True)
    report = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
              "dependencies": {p: version(p) for p in ("fastapi", "uvicorn", "anthropic", "openai", "httpx", "pydantic", "anyio", "jsonschema", "python-dotenv")}}
    report["offline"] = offline()
    if args.live:
        from dotenv import load_dotenv
        load_dotenv(args.env_file, override=False)
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key or key == "replace-with-your-deepseek-api-key":
            report["live"] = {"passed": False, "reason": "Missing DEEPSEEK_API_KEY"}
            print("[FAIL] LIVE: set DEEPSEEK_API_KEY or provide --env-file")
        elif not report["offline"]["passed"]:
            report["live"] = {"passed": False, "reason": "Offline verification failed; live not attempted"}
        else:
            report["live"] = asyncio.run(live())
    else:
        report["live"] = {"executed": False, "reason": "Use --live for real provider evidence"}
    ok = report["offline"]["passed"] and (not args.live or report["live"]["passed"])
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nOverall: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
