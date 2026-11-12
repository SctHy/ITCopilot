# IT-Copilot Frontend

Vue 3 + Vite 前端，对接本仓库的 IT-Copilot API。

| 页面 | 内容 |
|---|---|
| 对话 | SSE 流式回复、意图 / 工具调用追踪、审批状态 |
| 共享事项 | 申请人、审批人与 Agent 在同一事项中讨论、审批与跟进 |
| 知识库 | 混合检索调试、文档录入与 TXT / Markdown / JSON 导入 |
| 审批 | 按状态查看申请，批准 / 拒绝 / 撤回 / 提前归还 |
| 运行状态 | 实时 SLO 燃烧率、告警与处置建议、Agent / 工具 / MCP 健康度（每 15 秒刷新） |

## 本地开发

```bash
npm ci
npm run dev -- --host 127.0.0.1 --port 5173   # 默认连接 http://127.0.0.1:8001
npm run typecheck                             # vue-tsc
npm test                                      # node:test
npm run build
```

API 地址可在页面右上角“连接配置”中修改，也可用 `VITE_PYTHON_API_URL` 覆盖默认值。身份来自登录令牌（dev 模式演示身份或 Keycloak），写入知识库、处理审批等权限由服务端按角色判定。

## 代码结构

| 目录 | 职责 | 依赖方向 |
|---|---|---|
| `src/api/` | HTTP 调用：`http.ts` 统一处理地址、令牌与错误，其余按业务分文件（chat、approvals、knowledge、tasks、system）；`settings.ts` 读写本地连接配置 | 只依赖 `lib/types.ts` 与 `lib/stateMachine.ts` |
| `src/lib/` | 不碰 DOM 的状态与规则：会话控制器（`chatSession`、`authSession`、`task*Controller`）通过注入的 transport 调接口，可直接用 node:test 测试；`taskClient.ts`、`approvalDecision.ts` 是把 `api/` 接到控制器上的适配层 | 控制器不 import `api/`，transport 由 `App.vue` / 页面注入 |
| `src/views/` | 页面：各自加载并持有本页数据，按“连接 + 身份”重建 | 依赖 `api/`、`lib/`、`components/` |
| `src/components/` | 可复用展示组件与共享事项子面板 | 通过 props / emits 与页面交互 |
| `src/App.vue` | 外壳：导航、连接与身份、跨页面共享的对话会话与监控数据 | — |

全部源码为 TypeScript（strict）。接口数据类型集中在 `src/lib/types.ts`，只列前端实际读取的字段。测试仍是 JS，由 Node 直接加载 `.ts` 源文件（类型擦除，要求 `erasableSyntaxOnly`）。

## Docker

由仓库根目录的 `docker-compose.yml` 构建：nginx 托管静态资源，并把 `/api/` 同源反向代理到 api 服务（对 SSE 关闭缓冲）。容器启动时 `docker/entrypoint.sh` 按 `PYTHON_API_URL`（默认 `/api`）生成 `runtime-config.js`。
