"""确定性模型路由：按用途配置候选，合规过滤后按顺序故障切换。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import time
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_ai.exceptions import (ModelUnavailableError, ModelConnectionError, ModelHTTPError, ModelRequestTimeout, ModelServerError)
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from sqlalchemy import func, select

from servicedesk_agent.infra.change_log import record_change
from servicedesk_agent.infra.db import read, tenants, turn_records
from servicedesk_agent.agent.llm import build_model, close_model, is_model_unavailable
from servicedesk_agent.infra.tenant_context import current_tenant
from servicedesk_agent.infra.monitor import metrics

logger = logging.getLogger(__name__)

ROUTES = ("intent_fallback", "agent_read", "agent_write", "summary", "rewrite_rerank", "judge")
DEFAULT_CONFIG = pathlib.Path(__file__).resolve().parents[3] / "config/model_routes.yaml"

MODEL_ERRORS = (TimeoutError, ModelUnavailableError, ModelConnectionError, ModelHTTPError, ModelRequestTimeout, ModelServerError)

class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Prices(ConfigModel):
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)


class CompactionBudget(NamedTuple):
    trigger_tokens: int
    keep_recent_tokens: int


class ModelConfig(ConfigModel):
    api: Literal["chat_completions", "responses"] = "chat_completions"
    default_base_url: str | None = None
    base_url_env: str = Field(min_length=1)
    key_env: str = Field(min_length=1)
    name: str = Field(min_length=1)
    prompt_cache_mode: Literal["auto", "openai_key", "anthropic_breakpoints"] = "auto"
    prompt_cache_scope: Literal["tenant", "conversation"] = "tenant"
    prompt_cache_retention: Literal["in_memory", "24h"] | None = None
    context_window: int = Field(default=128_000, gt=0)
    compact_reserve_tokens: int = Field(default=24_000, ge=0)
    keep_recent_tokens: int = Field(default=20_000, ge=0)
    price_per_mtok: Prices
    regions: list[str] = Field(min_length=1)
    data_classes: list[Literal["public", "internal", "confidential"]] = Field(min_length=1)
    supports: list[str]
    # 思考开关的厂商协议：deepseek 经 extra_body 开关（思考模式拒绝指定函数的 tool_choice，结构化输出依赖它，所以默认关闭）；
    # none 表示不发送任何思考参数，路由也不能为它配置思考档位。
    thinking_api: Literal["deepseek", "openai", "none"]

    @model_validator(mode="after")
    def validate_protocol(self):
        if self.compact_reserve_tokens >= self.context_window:
            raise ValueError("compact reserve must be smaller than context window")
        if self.thinking_api == "openai" and self.api != "responses":
            raise ValueError("OpenAI thinking requires the Responses API")
        return self


class RouteConfig(ConfigModel):
    candidates: list[str] = Field(min_length=1)
    timeout_s: float = Field(default=15, gt=0)
    thinking: Literal["disabled", "low", "high", "max"] = "disabled"
    max_rounds: int = Field(default=1, ge=1)
    turn_deadline_s: float = Field(default=30, gt=0)
    request_timeout_s: float = Field(default=15, gt=0)


class TenantConfig(ConfigModel):
    data_class: Literal["public", "internal", "confidential"]
    regions: list[str] = Field(min_length=1)


class RoutingConfig(ConfigModel):
    models: dict[str, ModelConfig]
    routes: dict[str, RouteConfig]
    tenants: dict[str, TenantConfig]

    @model_validator(mode="after")
    def valid(self):
        if "default" not in self.tenants or not set(ROUTES) <= self.routes.keys():
            raise ValueError("default tenant and all standard routes are required")
        for name, route in self.routes.items():
            for candidate in route.candidates:
                if candidate not in self.models:
                    raise ValueError(f"{name}: unknown model {candidate}")
                required = {"tools", "tool_choice_required"} if name.startswith("agent_") or name == "intent_fallback" else set()
                if not required <= set(self.models[candidate].supports):
                    raise ValueError(f"{name}/{candidate}: missing {required}")
                if route.thinking != "disabled" and self.models[candidate].thinking_api == "none":
                    raise ValueError(f"{name}/{candidate}: thinking {route.thinking} needs a model with a thinking API")
            for tenant, policy in self.tenants.items():
                if not any(self.compliant(self.models[c], policy) for c in route.candidates):
                    raise ValueError(f"{tenant}/{name}: no compliant candidate")
        return self

    @staticmethod
    def compliant(model: ModelConfig, tenant: TenantConfig) -> bool:
        return tenant.data_class in model.data_classes and bool(set(tenant.regions) & set(model.regions))


def model_defaults(model: ModelConfig) -> ModelSettings | None:
    """模型级默认值：不经路由设置的直接调用（摘要、裁判的 complete_text）也关闭 DeepSeek 思考。"""
    if model.api == "responses":
        settings = OpenAIResponsesModelSettings(openai_store=False)
        if model.prompt_cache_retention is not None:
            settings["openai_prompt_cache_retention"] = model.prompt_cache_retention
        return settings
    return ModelSettings(extra_body={"thinking": {"type": "disabled"}}) if model.thinking_api == "deepseek" else None


def thinking_settings(model: ModelConfig, level: str) -> dict:
    if model.thinking_api == "openai" and model.api == "responses":
        return {"max_tokens": 3000 if level == "disabled" else 16000, **model_defaults(model),
                "openai_reasoning_effort": {"disabled": "none", "max": "high"}.get(level, level)}
    if level == "disabled":
        return {"max_tokens": 3000, "temperature": .2, **(model_defaults(model) or {})}
    # 思考 token 计入输出上限；思考模式下 temperature 无效，不传。
    return {"max_tokens": 16000, "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": level}}


@dataclass(frozen=True)
class Routed:
    model: Model
    name: str
    route: str
    reason: str
    settings: ModelSettings
    max_rounds: int
    generation: int


class ModelRouter:
    def __init__(self, config_path=DEFAULT_CONFIG, *, engine=None, models: dict[str, Model] | None = None,
                 api_key="", base_url=None, model=""):
        from servicedesk_agent.agent.tooling.tool_manager import CircuitBreaker

        self.path = pathlib.Path(config_path)
        self.config = RoutingConfig.model_validate(yaml.safe_load(self.path.read_text()))
        self._mtime = self.path.stat().st_mtime_ns
        self.fingerprint = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.engine = engine
        self._clients = AsyncExitStack()
        self._constructor = (api_key, base_url, model)
        self.models = self._build_models() if models is None else dict(models)
        if set(self.models) != set(self.config.models):
            raise ValueError("injected models must match configured names")
        self.breakers = {name: CircuitBreaker() for name in self.models}
        self._cost_cache = {}
        self.defaults = {}
        self._forced = {}
        self.calls = defaultdict(int)

    def _build_models(self):
        api_key, base_url, override = self._constructor
        primary = self.config.routes["agent_write"].candidates[0]
        models = {}
        for name, config in self.config.models.items():
            endpoint = os.getenv(config.base_url_env) or config.default_base_url
            key = os.getenv(config.key_env, "")
            if name == primary:
                if base_url:
                    endpoint, key = base_url, api_key
                elif api_key:
                    key = api_key
            models[name] = build_model(key, endpoint, override if override and name == primary else config.name,
                                       api=config.api, settings=model_defaults(config),
                                       anthropic_breakpoints=config.prompt_cache_mode == "anthropic_breakpoints")
            self._clients.push_async_callback(close_model, models[name])
        return models

    async def aclose(self):
        await self._clients.aclose()

    def reload(self):
        from servicedesk_agent.agent.tooling.tool_manager import CircuitBreaker

        mtime = self.path.stat().st_mtime_ns
        if mtime == self._mtime:
            return
        # Remember this edit either way: a rejected file is reported once, not on every model call.
        self._mtime = mtime
        raw = self.path.read_bytes()
        try:
            candidate = RoutingConfig.model_validate(yaml.safe_load(raw))
        except (yaml.YAMLError, ValidationError) as exc:
            # 启动时配置错误直接失败；运行中改错只拒绝这次修改，继续使用上一份已校验的配置。
            logger.error("model route config rejected, keeping %s: %s", self.fingerprint[:12], type(exc).__name__)
            metrics.CONFIG_RELOADS.labels(config="model_routes", outcome="rejected").inc()
            if self.engine is not None:
                record_change(self.engine, "model_route_rejected", hashlib.sha256(raw).hexdigest(),
                              {"kept": self.fingerprint, "error": type(exc).__name__})
            return
        previous = self.config
        # File edits take precedence over overrides whose underlying policy changed.
        if candidate != previous:
            self.defaults.clear()
        self.config = candidate
        self.models = self._build_models()
        # An unrelated edit must not close an open circuit: a model whose own settings are unchanged
        # keeps its breaker (state, cooldown and generation, so in-flight calls still report to it).
        self.breakers = {name: self.breakers[name] if name in self.breakers and previous.models.get(name) == model
                         else CircuitBreaker() for name, model in candidate.models.items()}
        self.fingerprint = hashlib.sha256(raw).hexdigest()
        metrics.CONFIG_RELOADS.labels(config="model_routes", outcome="applied").inc()
        if self.engine is not None:
            record_change(self.engine, "model_route", self.fingerprint, {"routes": list(candidate.routes)})

    def force(self, route: str, name: str):
        if name not in self.config.routes[route].candidates:
            raise ValueError(f"{name} is not a candidate for {route}")
        self._forced[route] = name

    def replace_models(self, model: Model):
        """显式的评测模型注入；所有调用角色使用同一个确定性替身。"""
        self.models = dict.fromkeys(self.models, model)

    def _healthy(self, name: str) -> bool:
        from servicedesk_agent.agent.tooling.tool_manager import CircuitState
        breaker = self.breakers[name]
        return breaker.state == CircuitState.CLOSED or (
            breaker.state == CircuitState.OPEN and time.monotonic() - breaker.opened_at >= breaker.recovery_s)

    def preferences(self, route, tenant, model=None, thinking=None):
        cfg = self.config.routes[route]
        preferred, default_level = self.defaults.get(route, (None, None))
        level = thinking or default_level or cfg.thinking
        policy = self.config.tenants.get(tenant, self.config.tenants["default"])
        if model is not None and model not in self.config.models:
            raise ValueError("未知模型")
        choices = []
        for name in cfg.candidates:
            spec = self.config.models[name]
            if model is not None and name != model:
                continue
            if not self.config.compliant(spec, policy):
                if model is not None:
                    raise PermissionError("模型不符合租户数据或地域策略")
                continue
            if route.startswith("agent_") and not {"tools", "tool_choice_required"} <= set(spec.supports):
                continue
            if level != "disabled" and spec.thinking_api == "none":
                continue
            choices.append(name)
        if not choices:
            raise ValueError("模型或思考档位不适用于此路由")
        choices.sort(key=lambda name: name != preferred)
        return choices, level

    def pick(self, route: str, tenant: str | None = None, *, excluding: frozenset[str] = frozenset(),
             model=None, thinking=None, reason: str | None = None, conv_id: str | None = None) -> Routed:
        """tenant 缺省取当前请求的租户；无租户或系统范围按 default 计。"""
        tenant = current_tenant() if tenant is None else tenant
        if tenant in {None, "*"}:
            tenant = "default"
        self.reload()
        config = self.config.routes[route]
        order, level = self.preferences(route, tenant, thinking=thinking)
        if model is not None:
            # Validate the requested primary even if a healthy alternative is available.
            self.preferences(route, tenant, model, level)
            order.sort(key=lambda name: name != model)
        candidates = [name for name in order if name not in excluding and self._healthy(name)]
        if route in self._forced:
            candidates = [name for name in candidates if name == self._forced[route]]
        if not candidates:
            raise ModelUnavailableError(route, "no healthy compliant model")
        name = candidates[0]
        selected_reason = "primary" if name == order[0] else "failover"
        if route == "agent_write" and self.over_budget(tenant):
            selected_reason = "budget"
        if not self.breakers[name].allow():
            raise ModelUnavailableError(name, "circuit probe busy")
        settings = ModelSettings(timeout=config.request_timeout_s, **thinking_settings(self.config.models[name], level))
        spec = self.config.models[name]
        if spec.prompt_cache_mode == "openai_key" and (spec.prompt_cache_scope == "tenant" or conv_id is not None):
            scope = [self.models[name].model_name, tenant]
            if spec.prompt_cache_scope == "conversation":
                scope.append(conv_id)
            cache_key = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
            if spec.api == "responses":
                settings["openai_prompt_cache_key"] = cache_key
            else:
                settings.setdefault("extra_body", {})["prompt_cache_key"] = cache_key
        return Routed(self.models[name], name, route, reason or selected_reason, settings, config.max_rounds, self.breakers[name].generation)

    async def run(self, routed: Routed, operation, *, deadline_at: float | None = None):
        """Await one model call and feed its outcome to the model's breaker.

        A failure at or after ``deadline_at`` is the turn's own budget running out, not the model
        failing: it releases a half-open probe and surfaces as TurnDeadlineExceeded."""
        try:
            result = await operation
        except (asyncio.CancelledError, TurnDeadlineExceeded):
            self.breakers[routed.name].abandon_probe(routed.generation)
            raise
        except MODEL_ERRORS as exc:
            if deadline_at is not None and time.monotonic() >= deadline_at:
                self.breakers[routed.name].abandon_probe(routed.generation)
                raise TurnDeadlineExceeded("Agent turn deadline exceeded") from exc
            self.record(routed, success=not is_model_unavailable(exc))
            raise
        self.record(routed, success=True)
        return result

    def record(self, routed: Routed, *, success: bool):
        breaker = self.breakers[routed.name]
        self.calls[routed.route] += 1
        before = breaker.state
        if success:
            breaker.record_success(routed.generation)
        else:
            breaker.record_failure(routed.generation)
            if self.engine is not None and before != "open" and breaker.state == "open":
                record_change(self.engine, "model_circuit", routed.name, {"state": "open"})

    def over_budget(self, tenant: str) -> bool:
        if self.engine is None:
            return False
        now = time.time()
        cached = self._cost_cache.get(tenant)
        if cached is not None and now - cached[0] < 60:
            return cached[1]
        with read(self.engine) as db:
            quotas = db.scalar(select(tenants.c.quotas).where(tenants.c.id == tenant))
            if quotas is None or "daily_cost_usd" not in quotas:
                result = False
            else:
                today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                cost = db.scalar(select(func.coalesce(func.sum(turn_records.c.cost_usd), 0))
                    .where(turn_records.c.tenant == tenant, turn_records.c.created_at >= today))
                result = cost >= quotas["daily_cost_usd"]
        self._cost_cache[tenant] = (now, result)
        return result

    def compaction_budget(self, model_name: str) -> CompactionBudget:
        model = self.config.models[model_name]
        return CompactionBudget(model.context_window - model.compact_reserve_tokens, model.keep_recent_tokens)

    def model_fingerprint(self, name: str) -> str:
        value = self.config.models[name].model_dump()
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def cost(self, name: str, usage: dict) -> float:
        prices = self.config.models[name].price_per_mtok
        cached = usage.get("cache_read_tokens", 0)
        return ((usage.get("input_tokens", 0) - cached) * prices.input
                + cached * prices.cache_read + usage.get("output_tokens", 0) * prices.output) / 1_000_000


class TurnDeadlineExceeded(RuntimeError):
    pass


class DeadlineModel(WrapperModel):
    def __init__(self, model, *, deadline, request_timeout_s):
        super().__init__(model)
        self.deadline, self.request_timeout_s = deadline, request_timeout_s

    def _bounded_settings(self, settings):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TurnDeadlineExceeded("Agent turn deadline exceeded")
        timeout = min(self.request_timeout_s, remaining)
        return {**settings, "timeout": timeout, "request_timeout": timeout}

    async def request(self, messages, model_settings, model_request_parameters):
        settings = self._bounded_settings(model_settings)
        budget = asyncio.timeout(settings["request_timeout"])
        try:
            async with budget:
                return await self.wrapped.request(messages, settings, model_request_parameters)
        except TimeoutError as exc:
            if not budget.expired():
                raise
            raise ModelRequestTimeout(self.model_name, settings["request_timeout"]) from exc

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        settings = self._bounded_settings(model_settings)
        budget = asyncio.timeout(settings["request_timeout"])
        try:
            async with budget:
                async with self.wrapped.request_stream(messages, settings, model_request_parameters, run_context) as stream:
                    yield stream
        except TimeoutError as exc:
            if not budget.expired():
                raise
            raise ModelRequestTimeout(self.model_name, settings["request_timeout"]) from exc
