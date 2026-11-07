"""通过本关 HTTP/SSE 路由执行真实上游验收。"""
import asyncio
import json
from pathlib import Path
import httpx
from dotenv import load_dotenv
from jsonschema import Draft202012Validator
from main import app

SCHEMA = {"type":"object","properties":{"name":{"type":"string"},"age":{"type":"integer"}},"required":["name","age"],"additionalProperties":False}
FORMATS = [{"type":"json_object"},{"type":"json_schema","name":"person","schema":SCHEMA}]

async def main():
    results = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', timeout=90) as client:
        for model in ('deepseek-flash','deepseek-v4-pro'):
            for streaming in (True,False):
                for fmt in FORMATS:
                    row = dict(model=model,stream=streaming,format=fmt['type'])
                    try:
                        response = await client.post('/chat',json=dict(model=model,stream=streaming,response_format=fmt,prompt='提取信息：张三今年30岁。请只返回JSON对象，包含name和age。'))
                        response.raise_for_status()
                        if streaming:
                            events = [(f.splitlines()[0][7:],json.loads(f.splitlines()[1][6:])) for f in response.text.strip().split('\n\n')]
                            kinds = [k for k,d in events]
                            assert 'error' not in kinds, str(events[-1])
                            assert kinds[0]=='start' and kinds[-1]=='done' and kinds.count('done')==1, kinds
                            assert events[-1][1]['reason']=='stop'
                            content = ''.join(d['text'] for k,d in events if k=='delta')
                            row['delta_count'] = kinds.count('delta')
                            assert row['delta_count']>0
                        else:
                            result = response.json()
                            assert result['finish_reason']=='stop'
                            content = result['content']
                        parsed = json.loads(content)
                        Draft202012Validator(SCHEMA).validate(parsed)
                        assert parsed==dict(name='张三',age=30), parsed
                        row.update(passed=True,content=parsed)
                    except Exception as exc:
                        row.update(passed=False,error=str(exc))
                    results.append(row)
                    print(json.dumps(row,ensure_ascii=False),flush=True)
    Path('live_acceptance.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    if not all(r['passed'] for r in results):
        raise SystemExit(1)

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path)
    args=parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file)
    asyncio.run(main())
