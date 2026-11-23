# 性能与故障测试报告

测试日期：2026-11-23。测试环境：Apple M4、16 GiB 内存、macOS 27.0.1 arm64、CPython 3.14.6。Colima 本机回环端口运行 PostgreSQL 18.4（`pgvector/pgvector:0.8.3-pg18`）和 Redis 8.8.4（`redis:8.8.4-alpine`）。下表为默认参数下的单轮实测数据，分位数按样本升序取最近秩，三个场景独立计时。

| 测试场景 | 负载与运行环境 | 实测结果 |
|---|---|---|
| `concurrency`（并发领取） | 独立 PostgreSQL schema，4 个异步 Worker，6 个权限资源；24 次授权 + 24 次到期回收 | 48 个任务耗时 0.664 s（72.25 任务/秒）。调度延迟 P50 / P95 / P99 为 38.394 / 94.557 / 105.976 ms。重复执行 0、重复外部写入 0、同资源写冲突 0，最终一致率 100% |
| `fault`（强杀故障恢复） | 独立 Worker 子进程 + 本机 HTTP 权限模拟服务，各场景使用独立 PostgreSQL schema，租约 1 秒 | 3/3 全部恢复一致。两次接管均先查外部真实状态，命中已生效权限 1 次，每轮实际外部写入恰好 1 次 |
| `sse`（断线续传与防重） | 真实 Redis 8.8.4 容器 + Uvicorn 回环 HTTP；24 个并发客户端，每条流 20 帧 | 480 帧全部收齐，0 丢失、0 重复。48 次同内容 POST 去重为 24 次执行，24/24 次篡改请求返回 409。断线重连至补齐 P50 / P99 为 93.234 / 102.235 ms |

## 故障恢复时间线

| 故障注入点 | 启动到进程被杀（ms） | 被杀到接管完成（ms） | 接管后查询外部状态的结果 |
|---|---:|---:|---|
| A · 发起外部权限请求前 `SIGKILL` | 731.87 | 2201.11 | 外部尚未授权，接管进程执行写入并二次核实 |
| B · 外部权限已生效、HTTP 响应丢失前 `SIGKILL` | 661.99 | 2117.76 | 外部已授权，接管进程直接确认成功，不重复写 |
| C · 正常执行对照组 | 1099.16 | — | 首次执行完成写入并核实 |

## 测试口径说明

- **并发领取（`concurrency`）**：从提交第一笔申请计时到最后一个任务成功。两批申请之间推进测试时钟，让第一批的到期回收任务与新授权任务同时竞争同一资源；统计的是实际运行耗时（不含模拟的授权时长）。调度延迟从任务入队或到期可执行时刻算起到 Worker 真正开始处理。测试用的目标模拟器会精确记录每个操作 ID 的写入次数、同资源并发写入重叠数以及最终权限状态。
- **强杀故障恢复（`fault`）**：父进程在预设同步点向 Worker 子进程发送真实 `SIGKILL`，同时本地 HTTP 模拟服务保持运行并记录实际收到的权限修改顺序。恢复耗时包含等待 1 秒租约过期和拉起新 Python 解释器的时间。该场景验证了进程突然死亡与响应丢失窗口，真实 Gitea 的网络抖动与鉴权异常需另行集成验证。
- **SSE 断线重连（`sse`）**：走真实的 API POST/GET 路由、防重绑定逻辑和 TCP 连接，用固定事件生成器替代大模型响应。每个客户端在收到一半消息时主动断开，再携带 `Last-Event-ID` 重连补齐到结束帧，逐条核对序号与事件 ID。重连延迟包含剩余帧的生成与传输耗时，不含真实大模型的首字延迟。
- **数据库并发路径**：并发测试真实走了 PostgreSQL 的 `FOR UPDATE SKIP LOCKED` 行锁领取路径，并校验至少有一个 Worker 通过 `LISTEN/NOTIFY` 被实时唤醒。

## 复现命令

在仓库根目录执行。默认 `sse` 命令依赖本机 PATH 中的 `redis-server`（会自动拉起临时私有 Redis 并在结束后清理）：

```sh
uv sync --locked
.venv/bin/python -m it_copilot.cmd.bench concurrency
.venv/bin/python -m it_copilot.cmd.bench fault
.venv/bin/python -m it_copilot.cmd.bench sse
make bench BENCH_ARGS=--json
make bench-concurrency BENCH_ARGS='--workers 2 --resources 3 --actions 12 --json'
make bench-sse BENCH_ARGS='--clients 4 --events-per-stream 8 --in-memory --json'
```

使用 Docker 中的真实 PostgreSQL 与 Redis 复现（使用独立 Compose 项目名 `itc-bench`，并发与故障命令会自动创建并清理随机 schema，不会污染业务表）：

```sh
# 先按 README 准备好 .env
docker compose -p itc-bench build api
docker compose -p itc-bench up -d --wait postgres redis
docker compose -p itc-bench run --rm --no-deps api sh -c \
  'python -m it_copilot.cmd.bench concurrency --database-url "$IT_COPILOT_DATABASE_URL" --json'
docker compose -p itc-bench run --rm --no-deps api sh -c \
  'python -m it_copilot.cmd.bench fault --database-url "$IT_COPILOT_DATABASE_URL" --json'
docker compose -p itc-bench run --rm --no-deps api sh -c \
  'python -m it_copilot.cmd.bench sse --redis-url "$REDIS_URL" --json'
docker compose -p itc-bench down
```
