# ServiceDesk Agent 前端

基于 React、TypeScript、TanStack Router 和 TanStack Query 构建，默认采用深锌灰暗色主题。侧边栏提供私人对话历史、审批中心和知识库管理，连接设置支持本地演示身份切换和 OIDC 登录。

```sh
npm ci
npm run dev
npm run check
```

`npm run check` 会依次执行 TypeScript 类型检查、Vitest 单元测试与界面测试以及 Vite 生产构建。后端启动方式见[项目主页 README](../README.md)。
