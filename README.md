# IT-Copilot

企业 IT 服务 Agent：检索知识、诊断问题、起草权限与工单申请。模型提出建议；可信身份、租户隔离、申请人确认、独立审批和执行由代码控制。

## 启动

Python 3.14，Node 版本跟随 [CI](.github/workflows/ci.yml)。

```bash
uv venv --python 3.14
uv pip install -r requirements-dev.txt
cp .env.example .env  # 配置模型凭据
./start_local.sh
```

前端 `http://127.0.0.1:5173`，API `http://127.0.0.1:8001/docs`。默认演示身份 alice，连接配置中切换 bob/carol 审批或 erin 管理；演示身份仅用于本地开发。容器部署用 `docker compose up -d --build`，前端端口 8080。

数据库仅支持 `0014` 当前基线及后续迁移。旧库需保留原处并改用新空库，例如 `.env` 设置 `IT_COPILOT_DATABASE_URL=sqlite:///data/it_copilot_current.sqlite3`；不要强改 revision 或清空业务数据。旧 Redis 工作集须隔离。见 [ADR 0040](docs/adr/0040-current-contracts-only.md)。

## 当前能力与边界

- 单 Agent、按用途模型路由、知识库混合检索、短期/长期记忆、模型出站脱敏。
- 私人聊天历史与 SSE 续传；共享事项支持成员、讨论、审批、待办、提醒、附件、检查点和目标核验。
- PostgreSQL/SQLite 持久工作流，租约/CAS、资源串行化、未知结果回查、限时授权回收；Redis 保存工作记忆及事件流。
- OIDC/Keycloak 身份，Gitea 权限沙箱、本地目录适配器、飞书入口、受控只读 MCP。
- 语音只追加到可编辑草稿，不自动发送。原音频传给 DeepSeek，文本脱敏不覆盖声音。

没有真实用户/生产效果证明；Gitea 是企业权限系统的沙箱替身。跨系统不宣称 exactly-once；取消不能撤回已到达外部系统的写请求。

## 开发入口

```bash
.venv/bin/python scripts/dev.py check --profile local
```

- [开发约定](AGENTS.md)、[环境与验证命令](CONTRIBUTING.md)
- [文档索引](docs/README.md)、[当前架构](docs/design-tradeoffs.md)、[状态与恢复](docs/state-machines.md)
- [评测结论与数据](docs/evaluation.md)：历史测量与当前测试分开
- [前端](frontend/README.md)、[安全边界](docs/threat-model.md)

默认只读上述入口和任务相关源码；不批量加载历史证据、数据集或所有 ADR。文档只维护结论、必要约束、数据口径与复现入口。
