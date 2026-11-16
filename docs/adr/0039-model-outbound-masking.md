# ADR 0039：模型出站脱敏与本地工具参数恢复

状态：已采纳。

模型出站在 HTTP 边界用会话隔离占位符替换敏感文本，受控工具参数可恢复，回复/存储继续脱敏。支持流式分片，未知占位符和不支持协议失败关闭。AstrLink Guard v0.1.3 INT8（revision 7216b6a3b897ef903d4b1ba367ae6fb95b0fa5e4，Apache-2.0），阈值 0.8，CPU；on 失败阻断，auto 允许规则降级，off 仅规则。缓存只存文本哈希与实体位置，不存原文；命中绕开推理锁，未命中仍串行。安装用 scripts/install_astrlink_guard.py。不能保护原始音频或保证识别完整；结果和漏检见 [脱敏数据](../model-masking-acceptance.md)。
