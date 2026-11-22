"""OpenTelemetry 链路追踪：一次 /chat 为根 span，PydanticAI 自动产生模型调用与工具调用子 span。

仅在设置 OTEL_EXPORTER_OTLP_ENDPOINT（OTLP gRPC 收集端）时启用；未启用时仍通过 W3C 上下文关联请求与异步动作。
默认不把 prompt / 模型输出写入 span（可能含员工信息），需要排障时设 IT_COPILOT_OTEL_INCLUDE_CONTENT=true。
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
import os
import secrets
from typing import Optional

from opentelemetry import context, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("it_copilot")


propagator = TraceContextTextMapPropagator()


def current_traceparent():
    carrier = {}
    propagator.inject(carrier)
    return carrier.get("traceparent")


def traceparent():
    return current_traceparent() or f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


@contextmanager
def trace_scope(parent, name):
    ctx = propagator.extract({"traceparent": parent or ""}, context=context.Context())
    if not trace.get_current_span(ctx).get_span_context().is_valid:
        ctx = propagator.extract({"traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"})
    incoming = trace.get_current_span().get_span_context()
    links = [trace.Link(incoming)] if incoming.is_valid else []
    token = context.attach(ctx)
    try:
        with tracer.start_as_current_span(name, links=links):
            yield traceparent()
    finally:
        context.detach(token)


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


def turn_attributes(record):
    span = trace.get_current_span()
    for key in ("agent_version", "model", "route", "policy_version", "tenant"):
        span.set_attribute("it_copilot." + key, record[key])
