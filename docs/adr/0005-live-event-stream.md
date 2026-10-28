# 0005 执行事件写入持久流，SSE 实时订阅并可续传

日期：2026-10-28 · 状态：已采纳

## 背景

v2 的 `/chat/stream` 先 `await chat()`，再回放计划、工具与正文分片：用户在整轮处理期间看不到任何进展，断线后也无法续读。

## 决定

- 每轮对话在后台任务中执行，执行中的事件（`intent`、`routed`、`plan`、`step_*`、`tool_started/finished`、`approval_required`、`content_delta`、`completed/error`）写入按请求划分的流：部署用 Redis Streams（`XADD`/`XREAD BLOCK`），无 Redis 时用进程内实现。
- SSE 每个事件带 `id`；`GET /chat/stream/{request_id}` + `Last-Event-ID` 续读。流归属调用者，他人读取返回 403。
- 执行与连接解耦：断开不取消执行；相同 `client_request_id` 重复提交只订阅已有执行。
- 事件负载脱敏并限制大小；正文仍是最终回答分片，不是模型 token 流。

## 后果

- 首事件时延改为由意图识别决定（实测见 docs/evaluation-v3.md）。
- 进程内 ASGI 传输会缓冲整个响应，测不出流式；时延基准与回归测试都走真实 uvicorn + TCP。
- 尚未实现模型 token 级流式（需要在工具循环中区分中间轮与最终轮）。
