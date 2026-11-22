# 开发与测试

后端使用 Python 3.14 和 uv。前端使用 Node 26 和 npm 11。

```sh
uv venv --python 3.14
uv pip install --python .venv/bin/python -e . --group dev
npm ci --prefix frontend
make lint test frontend-check
```

`make run-api` 与 `make run-worker` 分别启动 API 和执行器。二者应使用同一数据库及相同的适配器配置。开发环境可使用 SQLite，API 默认要求 Redis 可用。Compose 使用 PostgreSQL、pgvector 和 Redis。Worker 同时负责权限巡检与保留期清理。本地单进程演示可显式设置 `IT_COPILOT_EMBED_WORKER=true`。

离线测试使用临时 SQLite 数据库、fakeredis 和替代模型。PostgreSQL 回归可通过 `.venv/bin/python scripts/dev.py check --profile postgres` 运行，连接地址由 `IT_COPILOT_TEST_DATABASE_URL` 指向专用测试库。真实 OIDC、Gitea 和备份恢复的运行配置见 [CI](.github/workflows/ci.yml)。

[开发约定](AGENTS.md) · [架构与迁移](docs/README.md)
