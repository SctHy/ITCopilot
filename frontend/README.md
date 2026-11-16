# 前端

React 19、TypeScript、Vite、Tailwind v4；TanStack Router/Query；shadcn Base UI `base-nova`。保留 [组件来源与许可](vendor/shadcn-ui/README.md) 和 [布局来源与许可](vendor/shadcn-admin/README.md)。

```bash
npm ci
npm run dev -- --host 127.0.0.1 --port 5173
python3 ../scripts/dev.py check --profile frontend
```

API 默认 `http://127.0.0.1:8001`，连接配置或 `VITE_PYTHON_API_URL` 可覆盖；Docker 用 `/api` 同源代理，SSE 不缓冲。Node 26（最低 26.12）、npm 11，核心依赖采用精确版本并与 GCFeed 前端对齐。

| 模块 | 职责 |
|---|---|
| `src/api/` | HTTP、令牌、错误与协议转换 |
| `src/lib/` | 控制器与纯规则，transport 注入；不依赖 DOM |
| `src/hooks/useStore.ts` | React 订阅控制器状态 |
| `src/views/`、`src/components/` | 页面与业务组件 |
| `src/components/ui/` | Base UI 原始组件，保留来源许可 |
| `src/App.tsx`、`src/router.tsx` | 身份、布局、路由、查询缓存组装 |

当前路由：`/chat`、`/tasks?task=<id>`、`/tasks?new=true`、`/knowledge`、`/approvals`、`/operations`。根路径进入聊天；OIDC 回调先在原 URI 完成交换。

Query 管理列表快照；写请求不自动重试，不持久化 token/私有缓存。切换身份或服务地址时重建 QueryClient。SSE、审批和事项恢复仍使用原控制器的幂等键、续传、请求代次和草稿语义；不由 Query 替代。

语音在私聊和可写事项中追加到可编辑草稿，不自动发送。PCM WAV、120 秒/8 MiB；HTTPS/localhost、AudioWorklet。取消、切换会话/身份或失去权限后丢弃迟到结果；失败录音仅内存保留两分钟供重试。支持撤销本次转写，手动编辑后不再覆盖草稿。

服务端 `POST /speech/transcribe` 使用 Bearer 鉴权，凭据配置为 `DEEPSEEK_ASR_API_KEY`（仅 DeepSeek 官方基址允许回退 OPENAI_API_KEY）。原音频会传给 DeepSeek，文本脱敏不保护声音；取消等待不保证上游停止或免计费。

## 工程检查

`npm run check` 统一执行严格类型检查、Vitest 逻辑测试、Testing Library UI 测试和生产构建。`test` 使用 Node 环境，`test:ui` 使用 jsdom；两者共享 Vite 的 TS/TSX 转换与 `@/` 路径别名。`build` 自带类型检查，Docker 构建同样执行。TS 配置同时检查源码、TS 测试与 Vite/Vitest 配置，包含索引空值和未使用声明检查。

Query 查询将 AbortSignal 传入 API，身份或连接切换时取消旧查询并丢弃旧缓存。现有 SSE、审批与任务控制器继续管理业务流程；局部 UI 使用 React Hooks，不将控制器的 Proxy 状态容器推广为通用数据缓存。原 Node test runner 入口已经移除，现有逻辑测试保留原业务断言并改由 Vitest 运行。
