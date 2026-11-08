"""真实调用两个模型验证 JSON Object；会消耗少量 API 额度。"""
import argparse
import asyncio
import json
from pathlib import Path
import httpx
from dotenv import load_dotenv
from main import app

async def main(url=None):
    options = {"base_url": url} if url else {"transport": httpx.ASGITransport(app=app), "base_url": "http://test"}
    async with httpx.AsyncClient(timeout=90, **options) as client:
        for model in ("deepseek-flash", "deepseek-v4-pro"):
            response = await client.post("/chat", json={
                "model": model, "stream": False,
                "response_format": {"type": "json_object"},
                "prompt": '请返回一个 JSON 对象，包含 name（值为学习助手）和 lesson（值为12），不要其他文字。',
            })
            response.raise_for_status()
            result = response.json()
            assert result["model"] == model
            assert result["finish_reason"] == "stop"
            assert isinstance(result["content"], str)
            parsed = json.loads(result["content"])
            assert isinstance(parsed, dict)
            print(json.dumps(result, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="运行中的服务地址；不传则通过 ASGI 调用本关 HTTP 路由")
    parser.add_argument("--env-file", type=Path, help="仅加载密钥，不复制或输出")
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file)
    asyncio.run(main(args.url))
