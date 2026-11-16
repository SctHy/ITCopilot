# 0023 多租户：租户作用域、签发方绑定与行级安全

状态：部分条款已替代，以下为当前保留结论。

身份绑定 tenant 与 issuer/飞书工作区；审批子表、目录、任务、向量与缓存带租户作用域。应用代码强制过滤，隔离巡检覆盖读写。RLS 和原迁移链已移除，见 [0036](0036-multi-tenant-scope-reduction.md)、[0040](0040-current-contracts-only.md)。
