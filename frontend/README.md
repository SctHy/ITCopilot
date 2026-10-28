# IT-Copilot Frontend

Vue 3 + Vite 前端，对接本仓库的 IT-Copilot API。

| 页面 | 内容 |
|---|---|
| 对话 | SSE 流式回复、意图 / 路由 / 工具调用追踪、审批状态 |
| 知识库 | 混合检索调试、文档录入与 TXT / Markdown / JSON 导入 |
| 运行与评测 | 实时 SLO 燃烧率、告警与处置建议、Agent / 工具 / MCP 健康度，最新离线评测报告（每 15 秒刷新） |

## 本地开发

```bash
npm ci
npm run dev -- --host 127.0.0.1 --port 5173   # 默认连接 http://127.0.0.1:8001
npm test                                      # node:test
npm run build
```

API 地址可在页面右上角“连接配置”中修改，也可用 `VITE_PYTHON_API_URL` 覆盖默认值。服务端设置了 `IT_COPILOT_ADMIN_TOKEN` 时，在连接配置中填写管理员 Token 后才能写入知识库和处理审批。

## Docker

由仓库根目录的 `docker-compose.yml` 构建：nginx 托管静态资源，并把 `/api/` 同源反向代理到 api 服务（对 SSE 关闭缓冲）。容器启动时 `docker/entrypoint.sh` 按 `PYTHON_API_URL`（默认 `/api`）生成 `runtime-config.js`。
