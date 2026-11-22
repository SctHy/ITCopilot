# IT-Copilot

企业 IT 服务台。员工可以在对话中检索内部知识、诊断问题并起草权限或设备变更。模型出站请求经过脱敏。服务端从已验证的身份确定租户、申请人和可用操作，审批通过后由独立执行器处理变更。

## 本地演示

需要 Docker Compose。复制环境模板并填写 OpenAI-compatible 模型的地址、模型名和密钥。

```sh
cp .env.example .env
make up
```

打开 <http://127.0.0.1:8080>，通过连接配置选择演示员工。审批人使用独立身份查看已确认的申请。API 文档位于 <http://127.0.0.1:8001/docs>。首次启动会初始化 Gitea 沙箱并准备本地模型。使用 `make up-oidc` 启动 Keycloak，使用 `make down` 停止服务。

## 工程结构

| 目录 | 职责 |
| --- | --- |
| `src/it_copilot/cmd` | API、执行器、基准 CLI 入口 |
| `src/it_copilot/api` | 应用工厂、请求依赖、HTTP 与 SSE |
| `src/it_copilot/agent` | Agent 循环、模型路由、脱敏、工具、混合检索和记忆 |
| `src/it_copilot/workflow` | 审批、审计、持久执行、外部权限对账 |
| `src/it_copilot/infra` | 数据库、身份、租户、限流、事件流和监控 |
| `skills/it_helpdesk` | Agent 按需加载的服务台技能 |
| `frontend` | 对话、审批、知识库、身份切换与运行状态 |

[开发和测试](CONTRIBUTING.md) · [架构与数据迁移](docs/README.md)
