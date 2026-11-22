# 后端基准、备份与监控

## 混合负载

```bash
.venv/bin/python scripts/dev.py evaluate --file scripts/benchmark_backend.py --runs 40 --warmup 4 --concurrency 4
```

默认临时 SQLite。首次安装开发工具还需本机 `redis-server`；CI 明确安装该依赖。设置 `IT_COPILOT_BENCH_POSTGRES_URL` 为有 CREATE DATABASE 权限的专用测试 PostgreSQL 连接串时，脚本创建自己的 `itc_bench_<随机ID>` 数据库，结束时只删除本次创建的库。不会清空连接串中的数据库。Redis 使用独立临时 Unix socket 实例，需要本机 `redis-server`。

基准启动真实 loopback HTTP 服务并复用连接，混合执行事项列表、审批列表、申请人确认和独立审批，真实工作流 Worker 通过本地目录适配器执行。所有申请针对同一员工/资源，用于检验热点串行化。任务 Agent 关闭，模型调用直接报错；不消耗模型额度，也不接触 Gitea 或真实企业权限。

夹具在计时前创建。每轮四次请求，预热单独排除，输出每类 P50/P95/P99、实际请求数、吞吐、SQL 执行耗时和采样连接池占用，以及执行成功、审批审计唯一性断言。SQL 耗时不含连接池等待，池占用是离散采样；不是数据库瓶颈占比。HTTP 200 不等于业务完成，报告还等待全部本次审批执行成功。`.prom` 保存 API 侧指标原文。

2026-11-17 本机 PostgreSQL/真实 Redis 核验：4 并发，4 轮预热＋40 轮采样，160 个计时 HTTP 请求，无失败；44 个审批均执行成功且各有一条 approved 审计。这个短样本只作为工具与业务链路验收，不宣称生产容量。结果由 `scripts/dev.py evaluate` 保存至 `output/dev-workflow/`，其中 `passed` 才是业务断言结果。需要容量评估时再增加数据量、采样时长、轮次和固定到达率测试。

## 备份与隔离恢复

附件和向量已在应用 SQL 中；Redis 另有会话、回执和事件流，不能统称为可丢弃缓存。正式备份先停止 API、Worker、IM 等写入方，保持停止直到两个存储都快照完成。工具不自动暂停运行中的服务，也不宣称跨存储事务一致性。

```bash
# 环境中设置 IT_COPILOT_DATABASE_URL 与 REDIS_URL；不把凭据写进命令或提交仓库。
.venv/bin/python scripts/backup.py create output/backups/my-snapshot --quiesced
.venv/bin/python scripts/backup.py drill output/backups/my-snapshot
```

SQLite 使用在线 backup API 从只读源连接复制。PostgreSQL 使用 `pg_dump` custom 格式，并在同一个导出快照下记录表行数；本机需同主版本的 pg_dump/pg_restore，或指定 `--postgres-container <名称> --docker-context <context>` 在数据库容器内运行工具。容器模式适用于该容器本身的数据库，连接串用户/库名应与该实例一致。

PostgreSQL 恢复演练另需 `IT_COPILOT_BACKUP_ADMIN_URL`。它只创建随机 `itc_restore_test_...` 库，导入后核对全部表行数并删除；不启动任何业务 Worker，不重放审批或外部写入。Redis 使用新的临时 redis-server，校验恢复值，已过期键单独计数，不覆盖源实例。程序正常退出会清理本次临时资源；SIGKILL 后应按日志核查，勿批量删除其他同前缀资源。

备份目录 0700，产物 0600，包含敏感业务数据。manifest 校验数据库和 Redis 文件摘要，失败或不完整备份不能通过演练。`--without-redis` 仅用于明确接受不含 Redis 的备份，报告会注明；它不能恢复聊天续传与工作记忆。备份不包含 Keycloak、Gitea 等外部系统事实、模型文件和配置密钥；它不是跨系统灾备方案，也没有自动覆盖原库的恢复入口。

定向验证：`scripts/dev.py check --profile backend --tests tests/test_backup.py`。真实 PostgreSQL 快照测试额外设置 `IT_COPILOT_BACKUP_TEST_ADMIN_URL`；可用 `IT_COPILOT_BACKUP_TEST_CONTAINER`/`IT_COPILOT_BACKUP_TEST_CONTEXT` 指定工具所在容器。SQLite 与真实 Redis 测试正常运行，未配置 PostgreSQL 环境时该项明确跳过。

## 与 GCFeed 共用监控

GCFeed 提供现有 Prometheus/Grafana、两套仪表盘和告警规则。两项目使用同一 Docker context，先创建 `agent-monitoring` 网络；IT-Copilot 启动时追加 `-f deploy/monitoring.compose.yml`，GCFeed 追加其 `apps/monitoring/shared.compose.yml`。网络只连接 IT API 与共用 Prometheus，业务数据库和 Redis 独立。

IT 指标由 `/metrics` 暴露，专用网络别名为 `it-copilot-api:8001`。GCFeed 的 `shared.yml` 只在显式启用覆盖配置后增加该抓取目标。模型 Token、工具失败、聊天延迟和未知写结果积压显示在 IT-Copilot Overview；GCFeed 原仪表盘保持独立。部署时在 Prometheus Targets 验证两边真实 API 均 UP；规则测试通过不等于已部署连通。
