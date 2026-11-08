# 第 21 关最终验收记录

验收日期：**2026-11-08（Asia/Shanghai）**。基线：`20_RateLimit`。前 20 关目录未修改。

## 开发 Checklist：8 / 8

- [x] 建立独立最终目录，保留功能和全部历史测试，移除历史临时检查脚本与运行产物。
- [x] 修复 Flash 缓存 Token 统计；保留原始分类、未知值与 `usage_json`；不重算历史。
- [x] 提供固定直接依赖、完整依赖锁定文件、Python 版本说明与 `.env.example`。
- [x] README 覆盖架构、配置、字段、能力和边界。
- [x] curl 示例覆盖双模型、普通 / SSE、结构化、模板版本、指标、错误、限流。
- [x] `scripts/verify.py` 默认离线，复用全量测试，清晰汇总并以非零退出报告失败；支持 `--live`。
- [x] 完整 unittest、离线验收、新环境回归、HTTP 启动及真实模型冒烟通过。
- [x] 保存真实与模拟证据，说明限制，清理敏感及运行文件。

## 自动化结果

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| `python -m unittest discover -s tests -v` | **133 tests / OK**，约 5.0 秒 | 原有 129 项 + 新增 4 项 Token 用例；无跳过 |
| `python scripts/verify.py` | **133 passed, 0 failed；14/14 groups PASS** | [verification.json](evidence/verification.json) 的 offline |
| 新建独立 venv，安装 requirements.txt | 成功 | Python 3.13.3；完整解析结果已固定到 requirements.lock.txt |
| 新环境 `pip check` | **No broken requirements found** | 安装后依赖一致性检查 |
| 新环境 `python scripts/verify.py` | **133 passed, 0 failed** | [clean-environment.json](evidence/clean-environment.json) |
| 临时副本注入一个失败测试 | **退出码 1**，134 项中 1 项失败 | [verifier-failure.json](evidence/verifier-failure.json)；临时注入未进入交付目录 |
| 新环境 Uvicorn 单 worker，真实本机 HTTP | 页面与读接口 200；无效聊天请求 422，请求 ID 一致 | [startup.json](evidence/startup.json) |
| `python scripts/verify.py --live ...` | **4/4 真实请求 PASS** | [verification.json](evidence/verification.json) 的 live |

启动检查使用临时交付副本和空数据库，通过 README 的 Uvicorn 参数加载环境文件，访问 `/`、`/observability`、`/docs`、`/openapi.json`、`/metrics`、`/metrics/requests`，并检查 `/chat` 的无效模型请求。进程检查后关闭，无额外真实模型消耗。

测试环境有 Starlette 关于 HTTPX TestClient 的弃用提示，不影响本次测试；当前依赖已固定，未来升级依赖时需重新回归。

## 真实模型结果

四次真实调用使用现有本地 Key，未将 Key 复制到最终目录、报告或命令输出。请求仅包含合成冒烟文本。以下延迟只是本次样本，不代表性能承诺。

| 模型 | 请求 | 实际内容 | HTTP | 输入 / 输出 / 总 Token | 延迟 ms | TTFT ms |
| --- | --- | --- | --- | --- | --- | --- |
| Flash | 非流式普通文本 | `OK` | 200 | 21 / 1 / 22 | 892.1 | null |
| Flash | SSE JSON Schema | `{"ok":true}` | 200 | 87 / 5 / 92 | 684.5 | 634.7 |
| Pro | 非流式普通文本 | `OK` | 200 | 20 / 1 / 21 | 818.0 | null |
| Pro | SSE JSON Schema | `{"ok":true}` | 200 | 91 / 5 / 96 | 1172.9 | 1025.1 |

两次 SSE 均为 `start → delta × 5 → usage → done`，无 error；JSON 通过网关 Schema 校验和验收脚本内容检查。每个请求均在临时 Metrics 库有唯一成功记录，request ID 与 HTTP 头一致。完整 request ID、事件序列、原始 Flash 字段与统计值保存在 JSON 报告。

真实 Flash 返回了 `input_tokens`、`output_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens` 和 `service_tier`。本次两个短请求的缓存读取 / 创建字段均为 **0**，因此**真实冒烟没有证明缓存命中场景**。非零缓存与最终覆盖场景由真实 SDK + MockTransport 确定性验证：初始 100 / 200 / 50，最终 10 / 30 / 5，最终输入应为 45、输出为 2、总数为 47，不能累加初始快照；嵌套缓存创建细分不再重复求和。

首次受限沙箱执行曾因网络访问限制失败；随后获准联网，独立环境安装与四次调用成功。此失败没有被当作上游重试正确性的验收依据。

## 能力矩阵与证据范围

| 能力 | 模拟证据 | 真实证据 |
| --- | --- | --- |
| 双模型路由 | registry、complete、两个协议适配器 | Flash / Pro 均 HTTP 200 |
| SSE | HTTP 事件、异常终止、取消及关闭 | 两模型均有 start / delta / usage / done |
| 结构化输出 | JSON Object / Schema、非法 JSON、截断、流式失败 | 两模型 SSE JSON Schema 有效；JSON Object 由模拟覆盖 |
| 模板管理 | 创建、替换、并发版本、历史调用、跨进程 SQLite | 未额外发真实模板请求 |
| 可观测性 | SQLite 升级、持久化、完整 usage、首个非空 delta、缺失值 | 四次真实 Token / 延迟，SSE TTFT |
| 统一错误与重试 | MockTransport 401 / 429 / 503 / 超时，尝试上限、退避、取消 | 正常调用未用于证明重试 |
| 独立限流 | 可控单调时钟、并发争抢、429 / Retry-After、内部重试只扣一次 | 未向真实 Provider 制造限流 |

真实冒烟经过进程内 ASGI Gateway 向真实 Provider HTTPS 发请求，验证完整调用链与事件内容；ASGITransport 会缓冲响应，不能证明客户端 TCP 层逐块到达时间。单独启动检查验证真实 HTTP 可用性，不额外调用 Provider。资源释放、断连、故障后的重试与限流是可控自动化测试证据。

## 交付边界

已知限制详见 README：单进程内存限流；最多 3 次重试（共 4 次尝试）；流式 JSON 最终验证前可能已经发送增量；无用户鉴权及分布式状态；旧指标不重新计算；失败尝试未收到的 Token 不推算；日志可能含输出片段。未发现影响主要调用链的未解决严重 Bug。

最终目录不包含真实 Key、虚拟环境、运行数据库、日志或业务数据。证据只保留合成请求结果及环境版本。
