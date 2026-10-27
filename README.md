# IT-Copilot

企业内部 IT 服务台多智能体平台：把“描述问题”转成可解释的意图、计划、工具证据与审批动作。它不是普通 FAQ 或单轮 RAG，而是一个保留会话上下文、能编排多个专职 Agent、对写操作强制人工确认的工程化骨架。

## 架构

```mermaid
flowchart LR
  U[Web 前端] --> API[FastAPI /chat]
  API --> I[结构化意图识别]
  I --> P[Planner + 性能路由]
  P --> A[多 Agent 池 / Triage / Endpoint / Access / Network / Ticket]
  A --> G[PydanticAI Agent / 工具循环 / 结构化输出]
  G --> GW[8082 Anthropic 协议网关 或 OpenAI 直连]
  A --> T[工具治理 / Schema / TTL / timeout / circuit breaker]
  A --> R[混合 RAG / BM25 + Chroma + RRF]
  A --> M[三级记忆 / Redis + Chroma]
  T --> H{写操作审批门}
  H -->|approved| E[可替换企业适配器]
  H -->|pending/rejected| O[可解释结果]
  API --> X[/trace/request_id + /metrics]
```

## 核心能力

- 基于 PydanticAI 的结构化意图输出（`classify_it_request`）、置信度校准、多轮指代继承与低置信澄清。
- Triage、Endpoint、Access、Network/Software、Ticket 五类 Agent；支持性能路由与独立工具白名单。
- 向量、BM25、Hybrid 三种检索，Hybrid 使用 RRF 合并并返回 citation。
- 有界 Planner：依赖波次、并行执行、最多 6 步，工具结果进入统一 trace。
- Redis 分布式锁、`client_request_id` 幂等回执、工作/情景/画像三级记忆。
- 写操作默认 dry-run，经过 HITL 审批后才调用 mock 适配器。
- SSE 状态流、Prometheus 指标、可回放请求 trace、离线评测与安全回归。

## 技术栈

| 层 | 实现 |
|---|---|
| API | Python 3.14（uv 管理）、FastAPI、Pydantic |
| Agent 框架 | PydanticAI：Agent 工具循环、`Tool.from_schema`、`ToolOutput` 结构化输出、`UsageLimits` 轮数上限 |
| 模型接入 | 默认 PydanticAI AnthropicModel → 本地 Anthropic→OpenAI 协议网关（8082）；`IT_COPILOT_LLM_PROVIDER=openai` 时直连 OpenAI-compatible |
| 存储 | Redis（锁/幂等/工作记忆）、Chroma PersistentClient（向量记忆） |
| RAG | BM25、向量检索、RRF、部门过滤 |
| 前端 | Vue 3、Vite、原生 SSE |
| 质量与观测 | pytest、node:test、Prometheus、LLM-as-Judge 框架、离线评测脚本 |

## 可复现指标

指标均来自仓库中的固定数据集与 `evaluation/reports/*_latest.json`，不是生产流量估计。

| 项目 | 结果 | 口径 |
|---|---:|---|
| 意图校准集 | 47 条 | `evaluation/datasets/intent_v1.json` |
| Intent ECE | 0.394681 → 0.258583 | 温度缩放前/后 |
| Intent Brier | 0.168457 → 0.114710 | 温度缩放前/后 |
| RAG vector Recall@5 / MRR | 0.80 / 0.675 | 20 条固定查询 |
| RAG hybrid Recall@5 / MRR | 1.00 / 0.8417 | BM25 + vector + RRF |
| 记忆召回 / 偏好命中 | 1.00 / 1.00 | 4 条离线用例 |
| 无关注入率 | 0.00 | 记忆评测 |
| 安全回归通过率 | 4/4 | 脱敏、审批、失败不编造、部门边界 |
| 后端 / 前端测试 | 92 / 18 | 离线 pytest（LLM 用 PydanticAI FunctionModel 替身）/ node:test |

## 快速开始

```bash
cd IT-Copilot
uv venv --python 3.14 && uv pip install -r requirements-dev.txt   # start_local.sh 缺依赖时也会自动安装
cp .env.example .env
cp gateway/.env.gateway.example gateway/.env.gateway  # 填入未提交的兼容密钥
./start_local.sh
```

脚本会确保 Redis 容器 `it-copilot-redis`、8082 网关、8001 API 和 5173 前端就绪。若在 `.env` 中设置 `IT_COPILOT_LLM_PROVIDER=openai` 并填写 `OPENAI_BASE_URL`/`OPENAI_API_KEY`/`OPENAI_MODEL`，PydanticAI 会直连上游，脚本跳过网关。浏览器打开 <http://127.0.0.1:5173>，可尝试：`VPN 连不上`、`申请 GitLab 只读权限`、`查询我的工单`。API 文档：<http://127.0.0.1:8001/docs>。

分步启动可使用 `./gateway/run_gateway.sh`、`./run_local.sh` 与 `cd frontend && npm ci && npm run dev -- --host 127.0.0.1 --port 5173`。真实密钥只允许出现在未被 Git 跟踪的 `gateway/.env.gateway`。

## 评测与观测

```bash
python -m pytest -q
cd frontend && npm test && npm run build
cd .. && python scripts/evaluate_all.py
python scripts/calibrate_intents.py
```

运行时入口：`GET /metrics`、`GET /monitor`、`GET /trace/{request_id}`；流式接口：`POST /chat/stream`。SSE 先发送计划、步骤、工具与审批状态，再发送正文片段，仍复用原有锁、幂等和 trace。

## 项目结构

```text
api/          FastAPI 路由、SSE、审批与 trace
agents/       Agent profile、池化、路由、并行协作（PydanticAI Agent + 工具包装）
core/         意图、Planner、记忆/RAG 编排；core/llm.py 为 PydanticAI 模型工厂
memory/       Redis 锁、幂等、三级记忆
tooling/      Schema、TTL、超时、熔断、fallback
gateway/      本地协议转换网关
frontend/     Vue + Vite 前端
evaluation/   固定数据集与可复现报告
tests/        后端离线回归（当前 92 项）
docs/         设计取舍、截图与变更记录
```

## Mock 与生产替换边界

当前 AD/LDAP、ITSM、CMDB、审批中心、设备库存和状态页均为本地 mock；接入生产时应替换适配器，不改变 Agent/工具治理接口。所有写操作保持最小权限、默认不执行；密码、OTP、恢复码、私钥等敏感字段会脱敏且不回显。

## 文档

- [模块设计与 trade-off](docs/design-tradeoffs.md)
- [变更记录](CHANGELOG.md)

## 界面演示

<p align="center">
  <img src="docs/assets/01-readonly-rag.png" alt="只读问答" width="80%"><br>
  <em>只读问答：意图识别 + 混合检索 RAG，回答引用知识库证据</em>
</p>

<p align="center">
  <img src="docs/assets/02-slot-clarification.png" alt="缺槽位澄清" width="80%"><br>
  <em>缺槽位澄清：写操作信息不全时先反问，不执行任何写工具</em>
</p>

<p align="center">
  <img src="docs/assets/03-pending-approval.png" alt="待审批" width="80%"><br>
  <em>待审批（HITL）：写操作只生成 pending_approval，人工批准后才执行</em>
</p>

## License

MIT，见 [LICENSE](LICENSE)。
