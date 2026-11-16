# Base UI 组件来源

`frontend/src/components/ui/` 的 24 个组件采用 shadcn/ui 官方 `base-nova` 注册表，交互组件底层为 `@base-ui/react`。纯样式组件（如 Card、Skeleton）不需要交互基础库。

- 注册表：`https://ui.shadcn.com/r/styles/base-nova/<name>.json`
- 清单及注册表响应 SHA-256：[base-nova.json](base-nova.json)
- shadcn/ui 许可：[MIT](LICENSE)
- Base UI 依赖版本由 `frontend/package-lock.json` 固定。
- `frontend/components.json` 的 `style` 为 `base-nova`，后续 CLI 添加组件延续同一底层。

## 本地适配

- 注册表中的 `cn` 和内部路径映射到本项目 `lib/utils`、`components/ui`、`hooks`。
- 图标占位符转换为项目已有的 Lucide 图标；`cn-font-heading` 对应本项目 `font-heading`，菜单预设标记移除，RTL 图标翻转使用 Tailwind 类。
- 业务组件使用 Base UI 的 `render` 组合方式、Tooltip `delay` 和 Accordion `multiple`。不保留 Radix `asChild` 兼容层。
- 单选传入 `items` 以显示标签，多选直接使用 Select `multiple`；弹出列表在触发器下方展示。
- 手风琴动画使用 Base UI 的 `--accordion-panel-height`；应用内容沿用 SidebarInset 的 main 地标，避免嵌套 main。
- 按钮加载状态继续由业务调用方的 `aria-busy`、`disabled` 和 CSS 表达，权限和请求状态继续由原业务控制器管理。

后台主题和页头的来源单独记录在 [shadcn-admin 说明](../shadcn-admin/README.md)。

本地视觉调整：按钮、标签页和侧栏仅过渡颜色；状态徽标采用小圆角；弹窗和抽屉使用半透明遮罩，不使用背景模糊。组件交互继续由 Base UI 管理。
