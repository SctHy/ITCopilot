# 开发与验证

开发约束以 [AGENTS.md](AGENTS.md) 为准。先查看工作区 diff，保留其他任务修改；明确验收条件，读相关实现/测试/ADR，小步修改后验证。普通变更用对话或 PR 交接，不生成过程报告。

## 环境与命令

Python 3.14，Node 跟随 [CI](.github/workflows/ci.yml)。

```bash
uv venv --python 3.14
uv pip install -r requirements-dev.txt
npm --prefix frontend ci --no-audit --no-fund
.venv/bin/python scripts/dev.py check --profile local
```

| profile | 覆盖 | 使用条件 |
|---|---|---|
| docs | 文档相对文件链接 | 文档改动；正文事实需人工核对 |
| backend | 文档、Ruff、全部离线 pytest | 后端改动 |
| frontend | 类型、逻辑/UI 测试、构建 | 前端改动 |
| local | backend + frontend | 跨端改动 |
| postgres | PostgreSQL/pgvector 契约与并发 | 数据库、审批、租约、回收改动 |
| integration | 真实 Keycloak/Gitea 沙箱 | 身份或外部适配器改动 |

命令每次只选一个 profile。开发中可加 `--tests tests/test_x.py`，它只代表选定测试；交付仍跑相关完整 profile。`--dry-run` 查看命令，`--timeout` 设置每条命令秒数（默认 900）。

postgres 要求专用 `IT_COPILOT_TEST_DATABASE_URL`；integration 要求 `IT_COPILOT_TEST_OIDC_ISSUER`、`IT_COPILOT_TEST_GITEA_URL`、`IT_COPILOT_TEST_GITEA_PASSWORD`。沙箱启动步骤见 CI。禁止连接业务库作测试。

backend 禁止 dotenv 加载，默认 SQLite、模拟模型和 fakeredis；不是网络沙箱。真实模型效果需按改动另跑评测，不能用离线通过代替。缺少外部环境应明确报告未验证。

## 验证产物

`output/dev-workflow/<运行编号>/` 保存命令、退出码、日志、统计及运行前后源码指纹；源码在验证期间变化即失败。指纹不含 `.env`，不能替代依赖与外部环境记录。

```bash
.venv/bin/python scripts/dev.py snapshot
.venv/bin/python scripts/dev.py evaluate scripts/evaluate_application.py --limit 1000
.venv/bin/python scripts/dev.py inventory
.venv/bin/python scripts/dev.py clean
.venv/bin/python scripts/dev.py clean --days 30 --apply
```

支持 `--output` 的评测交给 `evaluate` 管理输出，不另传 `--output`；单文件输出在脚本名前加 `--file`。`completed` 只代表命令结束，业务断言另看结果。评测口径与固定输入见 [evaluation](docs/evaluation.md)。

`clean` 默认预览，仅处理本工具超过 30 天的临时运行；保留 `.keep`、运行中、Git 跟踪、人工改动和未知文件。不会清理源码、业务数据、固定输入或证据包。`inventory` 的文字引用只供复核，不能单独作为删除依据。

## 文件规则

- 现行说明每个主题一个入口，只保留结论、约束、数据分母/条件及复现入口；删重复叙事、已完成计划和日志。
- ADR 保留编号与短决定；被替代内容仅保留替代关系。开发时只读涉及的 ADR。
- 测试按当前业务契约保留，不按年龄删，不放宽断言或扩大 skip 掩盖失败。
- 原始证据与大索引压缩存放，按需校验和恢复，见 [证据入口](docs/archive/README.md)。本机 `local/` 证据不会随 Git 克隆。
- 月度 [maintenance](.github/workflows/maintenance.yml) 只读盘点；CI 产物保留 14 天。长期基线/失败先固定，再清理临时副本。
- 仅支持 [当前数据库和格式](docs/adr/0040-current-contracts-only.md)，后续迁移从 0015 继续；发布/回退不能撤销已执行的外部事实。提交、推送、部署按授权范围执行。
