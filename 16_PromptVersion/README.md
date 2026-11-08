# 第 16 关：Prompt 模板版本管理与持久化

基于第 15 关，模板改为 SQLite 持久化。Adapter、SSE、JSON Object / JSON Schema 处理逻辑及正则单次变量替换保持不变。

## 启动

在本目录设置 `DEEPSEEK_API_KEY`，运行：

```powershell
..\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 8012
```

首次访问模板时自动创建本目录的 `prompts.db`，路径由 `Path(__file__)` 确定，与启动工作目录无关。默认 `translate`、`summarize` 通过 `INSERT OR IGNORE` 写入版本 1，重复初始化不会增加版本或覆盖内容。数据库及日志已加入本目录的忽略规则。

## 创建模板

`POST /templates`：

```json
{"template_id":"greeting","content":"请用{{language}}向{{name}}问好"}
```

返回：

```json
{"template_id":"greeting","version":1}
```

每次保存都创建一个新版本，包括相同内容；各模板独立从 1 递增。`BEGIN IMMEDIATE` 将查询最大版本和插入放在同一事务中，复合主键防止重复版本。ID 和内容必须是非空白字符串；非法输入返回 422。此接口不接受客户端指定版本。

## 使用模板

`POST /chat`：

```json
{
  "model":"deepseek-flash",
  "template_id":"greeting",
  "template_version":1,
  "variables":{"language":"日语","name":"张三"},
  "stream":false
}
```

省略 `template_version`（或传 null）使用最新版；指定时必须是大于等于 1 的整数，不接受布尔值、小数或字符串。只有提供 `template_id` 时才能设置版本。

`prompt`、`messages`、`template_id` 仍然三选一。不存在的模板或版本、缺少变量、渲染结果为空或超过 2000 字符均在调用模型前返回 422。额外变量被忽略，变量值保持字面含义，不递归替换。流式及 `response_format` 用法与第 15 关一致。

## Python 接口

- `init_db()`：幂等创建表和默认模板。
- `save_template(template_id, content) -> int`：保存并返回新版本号。
- `get_template(template_id, version=None) -> str`：获取最新或历史内容，不存在抛出 ValueError。
- `render_prompt(template_id, variables, version=None) -> str`：查询后复用原有变量校验和单次替换。

## 验证

在本目录运行：

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖版本递增、相同内容重复保存、历史及最新查询、并发保存、复合主键、默认初始化幂等、跨进程重启持久化、不同工作目录的路径稳定性、HTTP 422 和模型收到的实际提示词。

保留上一关的普通 prompt、多轮 messages、两个模型、SSE、JSON Object 和 JSON Schema 回归。测试使用隔离临时数据库及模拟 HTTP，不访问远程模型服务。
