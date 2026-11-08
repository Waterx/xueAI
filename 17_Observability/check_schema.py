"""真实调用 Flash / Pro 验收 JSON Schema；会消耗 API 额度。"""
import argparse
import asyncio
import json
from pathlib import Path

import httpx
from dotenv import load_dotenv
from jsonschema import Draft202012Validator, validate

from main import app

FORMAT = {
    "type": "json_schema", "name": "person",
    "schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"], "additionalProperties": False,
    },
}


async def main(url=None, regression=False):
    options = {"base_url": url} if url else {"transport": httpx.ASGITransport(app=app), "base_url": "http://test"}
    async with httpx.AsyncClient(timeout=90, **options) as client:
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            response = await client.post("/chat", json={
                "model": model, "stream": False, "prompt": "提取信息：张三今年30岁。",
                "response_format": FORMAT,
            })
            response.raise_for_status()
            result = response.json()
            assert result["model"] == model and result["finish_reason"] == "stop"
            assert isinstance(result["content"], str)
            data = json.loads(result["content"])
            validate(instance=data, schema=FORMAT["schema"], cls=Draft202012Validator)
            assert data == {"name": "张三", "age": 30}
            print("json_schema " + json.dumps(result, ensure_ascii=False), flush=True)
            if regression:
                for mode in ("json_object", "text", "stream"):
                    request = {"model": model, "prompt": '请返回 JSON 对象 {"ok":true}。', "stream": mode == "stream"}
                    if mode == "json_object":
                        request["response_format"] = {"type": "json_object"}
                    response = await client.post("/chat", json=request)
                    response.raise_for_status()
                    if mode == "stream":
                        assert "event: error" not in response.text
                        assert 'event: delta' in response.text and 'event: done' in response.text
                        assert '"reason": "stop"' in response.text
                    else:
                        assert response.json()["finish_reason"] == "stop"
                        if mode == "json_object":
                            assert isinstance(json.loads(response.json()["content"]), dict)
                    print(f"{model} {mode} 回归通过", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="运行中的服务地址；省略时通过本关 ASGI 路由调用真实上游")
    parser.add_argument("--env-file", type=Path, help="仅加载密钥，不复制或输出")
    parser.add_argument("--regression", action="store_true", help="同时真实验证 json_object、普通文本和 SSE")
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file)
    asyncio.run(main(args.url, args.regression))
