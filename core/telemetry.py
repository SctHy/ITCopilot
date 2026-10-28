"""OpenTelemetry 链路追踪：一次 /chat 为根 span，PydanticAI 自动产生模型调用与工具调用子 span。

仅在设置 OTEL_EXPORTER_OTLP_ENDPOINT（如 http://jaeger:4317）时启用；未启用时 tracer 为 no-op，零开销。
默认不把 prompt / 模型输出写入 span（可能含员工信息），需要排障时设 IT_COPILOT_OTEL_INCLUDE_CONTENT=true。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from opentelemetry import trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("it_copilot")


def setup_tracing(exporter=None):
    """配置全局 TracerProvider 并为所有 PydanticAI Agent 打开埋点；返回 provider 供关闭时 flush。"""
    if exporter is None and not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    provider = TracerProvider(resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "it-copilot")}))
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    include_content = os.getenv("IT_COPILOT_OTEL_INCLUDE_CONTENT", "false").lower() in {"1", "true", "yes"}
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, include_content=include_content))
    global tracer
    tracer = provider.get_tracer("it_copilot")
    logger.info("OpenTelemetry tracing enabled (include_content=%s)", include_content)
    return provider


def shutdown_tracing(provider: Optional[object]) -> None:
    if provider is None:
        return
    from pydantic_ai import Agent
    Agent.instrument_all(False)
    provider.shutdown()


def chat_span(request_id: str):
    return tracer.start_as_current_span("it_copilot.chat", attributes={"it_copilot.request_id": request_id})
