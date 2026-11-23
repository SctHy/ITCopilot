# 验收基准

2026-11-23，Apple M4，16 GiB，macOS 27.0.1 arm64，CPython 3.14.6。Colima 回环端口运行 PostgreSQL 18.4（pgvector/pgvector:0.8.3-pg18）及 Redis 8.8.4（redis:8.8.4-alpine）。下表为默认负载的单轮实测。表内分位数按样本升序取最近秩；三个场景各自独立计时。

| 场景 | 负载与后端 | 结果 |
|---|---|---|
| concurrency | 独立 PostgreSQL schema，4 个异步 Worker，6 个权限资源；24 个授权和 24 个到期回收 | 48 个动作，0.664 s，72.25 actions/s。调度 P50/P95/P99 为 38.394 / 94.557 / 105.976 ms。重复执行 0，重复外部写 0，同资源写冲突 0，最终一致率 100% |
| fault | 独立 Worker 子进程与回环 HTTP 权限服务，场景各用独立 PostgreSQL schema，租约 1 s | 3/3 收敛。两次接管均先回查，命中已生效操作 1 次。每轮外部写恰好 1 次 |
| sse | 真实 Redis 8.8.4 容器，Uvicorn 回环 HTTP；24 个客户端，每流 20 帧 | 480 帧全部收齐，丢帧 0、重复 0。48 个同内容 POST 触发 24 次执行，另有 24/24 个篡改 POST 返回 409。重连至补齐 P50/P99 为 93.234 / 102.235 ms |

## 故障时间线

| 注入点 | 启动至进程退出（ms） | 退出至接管验收（ms） | 接管回查结果 |
|---|---:|---:|---|
| A · 发权限请求前 SIGKILL | 731.87 | 2201.11 | 尚未授权，接管者执行写入并独立验证 |
| B · 外部权限已生效、HTTP 响应尚未返回时 SIGKILL | 661.99 | 2117.76 | 已授权，接管者直接提交结果 |
| C · 正常执行至退出 | 1099.16 | — | 首次执行完成写入与验证 |

## 计量边界

- **并发**从提交第一份申请计到最后一个动作成功。两批申请之间推进业务时钟，让第一批到期回收与新授权竞争；真实耗时取单调时钟，不含模拟的授权期限。调度延迟从授权动作的入队时间或回收动作被推进至可执行的时刻计至 Worker 开始处理。内存目标记录每个操作 ID 的写入次数、同资源在途写重叠，以及每个资源最终权限。
- **故障**由父进程在同步屏障处杀死指定 Worker，HTTP 服务继续保存权限和调用顺序。恢复时间包含等待租约到期和启动新解释器。单机进程死亡覆盖了响应未知窗口；真实 Gitea 的网络、鉴权和故障分布需单独验证。
- **SSE**使用产品 POST 和 GET 路由、流绑定和真实 TCP 连接。确定性事件生产器替代模型；客户端在中点关闭连接，携游标补收到终帧，再逐帧检查序号及 ID。恢复延迟包括剩余帧生成与传输。该指标不衡量模型首 token 延迟。
- 并发场景走 PostgreSQL 的行锁领取路径（FOR UPDATE SKIP LOCKED），并断言至少一个 Worker 收到 LISTEN/NOTIFY 通知。测试目标在进程内模拟权限写入，故障场景使用回环 HTTP 权限服务。

## 复现

在仓库根目录运行。默认 SSE 需要 PATH 中存在 redis-server；CLI 会创建私有 Unix socket 并在结束时关闭进程。

```sh
uv sync --locked
.venv/bin/python -m it_copilot.cmd.bench concurrency
.venv/bin/python -m it_copilot.cmd.bench fault
.venv/bin/python -m it_copilot.cmd.bench sse
make bench BENCH_ARGS=--json
make bench-concurrency BENCH_ARGS='--workers 2 --resources 3 --actions 12 --json'
make bench-sse BENCH_ARGS='--clients 4 --events-per-stream 8 --in-memory --json'
```

Docker 复现使用独立 Compose 项目，只启动 PostgreSQL 与 Redis。在后端镜像中执行基准，连接地址取自 Compose 内网配置。使用未运行应用迁移的专用测试库；并发和故障命令创建并清理随机 schema，pgvector 扩展保留在测试库。外部 Redis 使用独立键前缀与过期时间。

```sh
# 先按 README 准备 .env
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
