"""运行时组件：由 api/main.py 的 lifespan 组装后写入，路由在请求时读取。

集中在一个模块里，测试与评测脚本替换组件时只需 ``monkeypatch.setattr(state, "workflow", ...)``。
"""
from typing import Any, Optional

from core.events import InMemoryEventBus

lifecycle: Any = None
orchestrator: Any = None
memory: Any = None              # MemoryManager；Redis 不可用且显式允许降级时为 None
tool_manager: Any = None
kb: Any = None
skills: Any = None
profile_updater: Any = None
tracing: Any = None
monitor: Any = None
mcp_client: Any = None
workflow: Any = None            # WorkflowStore：审批与执行状态的唯一来源
worker: Any = None
tasks: Any = None               # shared service rooms, distinct from private conversations
task_worker: Any = None
task_event_pump: Any = None
retention: Any = None          # core.retention.RetentionSweeper
sessions: Any = None           # core.session_revocation.SessionRevocations；None 时不检查吊销
rate_limiter: Any = None       # core.rate_limit；None 表示不限流（单元测试默认）
task_capabilities: Any = None
registry: Any = None
channels: Optional[Any] = None  # IM 渠道服务（配置 FEISHU_APP_ID 时启用）
feishu: Optional[Any] = None    # FeishuClient
# 未接 Redis 时（单元测试、显式降级）使用进程内事件流；lifespan 中有 Redis 则替换为 Redis Streams。
events: Any = InMemoryEventBus()
# 保存后台任务引用：事件循环只持有弱引用，未被引用的任务可能在执行中途被回收。
background_tasks: set = set()

# Read-only cross-conversation workflow recall, injected independently from private memory.
task_recall = None
# The caller's own withdrawable requests and releasable grants (agents/request_recall.py).
request_recall = None
