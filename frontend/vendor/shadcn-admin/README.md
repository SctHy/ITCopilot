# 后台布局来源

- 仓库：https://github.com/satnaing/shadcn-admin
- 固定提交：`e16c87f213a5ba5e45964e9b67c792105ec74d26`
- 许可：[MIT](LICENSE)，保留原版权与许可文本。
- 保留的代码：`src/components/layout/header.tsx`、`src/hooks/use-mobile.tsx`，以及 `src/styles.css` 的基础主题；业务外壳继续采用后台侧边栏与页头布局。
- `src/components/ui/` 已整体迁移到官方 shadcn/ui 的 Base UI `base-nova` 实现，包括 Sidebar。组件来源与变更见 [Base UI 来源说明](../shadcn-ui/README.md)。

主题补充 IT 业务的成功、警告、信息颜色和消息样式；状态框使用中性背景，状态点和图标保留语义色。页头使用不透明背景和滚动后的分隔线。模拟登录、Clerk、示例聊天数据和演示业务页面未接入；认证使用本项目 dev token / Keycloak PKCE，授权和审批由服务端决定。
