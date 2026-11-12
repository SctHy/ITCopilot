"""确定性模型路由：合规和健康是硬约束，偏好顺序由离线评测门禁约束。"""
from __future__ import annotations

import hashlib
import os
import pathlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.exceptions import (ModelUnavailableError, ModelConnectionError, ModelHTTPError, ModelRequestTimeout, ModelServerError)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from sqlalchemy import func, select

from core.db import read, tenants, turn_records
from core.llm import build_model, is_model_unavailable
from evaluation.reporting import percentile

ROUTES = ("intent_fallback", "agent_read", "agent_write", "summary", "profile", "rewrite_rerank", "judge")
DEFAULT_CONFIG = pathlib.Path(__file__).resolve().parents[1] / "config/model_routes.yaml"

MODEL_ERRORS = (TimeoutError, ModelUnavailableError, ModelConnectionError, ModelHTTPError, ModelRequestTimeout, ModelServerError)

class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Prices(ConfigModel):
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)


class ModelConfig(ConfigModel):
    base_url_env: str = Field(min_length=1)
    key_env: str = Field(min_length=1)
    name: str = Field(min_length=1)
    price_per_mtok: Prices
    regions: list[str] = Field(min_length=1)
    data_classes: list[Literal["public", "internal", "confidential"]] = Field(min_length=1)
    supports: list[str]


class RouteConfig(ConfigModel):
    candidates: list[str] = Field(min_length=1)
    timeout_s: float = Field(default=15, gt=0)
    thinking: Literal["disabled", "low", "high", "max"] = "disabled"
    max_rounds: int = Field(default=1, ge=1)
    latency_slo_s: float | None = Field(default=None, gt=0)
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
            for tenant, policy in self.tenants.items():
                if not any(self.compliant(self.models[c], policy) for c in route.candidates):
                    raise ValueError(f"{tenant}/{name}: no compliant candidate")
        return self

    @staticmethod
    def compliant(model: ModelConfig, tenant: TenantConfig) -> bool:
        return tenant.data_class in model.data_classes and bool(set(tenant.regions) & set(model.regions))


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
        from tooling.tool_manager import CircuitBreaker

        self.path = pathlib.Path(config_path)
        self.config = RoutingConfig.model_validate(yaml.safe_load(self.path.read_text()))
        self._mtime = self.path.stat().st_mtime_ns
        self.fingerprint = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.engine = engine
        self._constructor = (api_key, base_url, model)
        self.models = self._build_models() if models is None else dict(models)
        if set(self.models) != set(self.config.models):
            raise ValueError("injected models must match configured names")
        self.breakers = {name: CircuitBreaker() for name in self.models}
        self.latencies = defaultdict(deque)
        self._cost_cache = {}
        self._forced = {}
        self.calls = defaultdict(int)

    def _build_models(self):
        api_key, base_url, override = self._constructor
        primary = self.config.routes["agent_write"].candidates[0]
        return {name: build_model(api_key or os.getenv(config.key_env, ""),
                                 base_url or os.getenv(config.base_url_env),
                                 override if override and name == primary else config.name)
                for name, config in self.config.models.items()}

    def reload(self):
        from tooling.tool_manager import CircuitBreaker

        if self.path.stat().st_mtime_ns == self._mtime:
            return
        candidate = RoutingConfig.model_validate(yaml.safe_load(self.path.read_text()))
        self.config = candidate
        self.models = self._build_models()
        self.breakers = {name: CircuitBreaker() for name in self.models}
        self.latencies.clear()
        self._mtime = self.path.stat().st_mtime_ns
        self.fingerprint = hashlib.sha256(self.path.read_bytes()).hexdigest()
        if self.engine is not None:
            from core.change_log import record_change
            record_change(self.engine, "model_route", self.fingerprint, {"routes": list(candidate.routes)})

    def force(self, route: str, name: str):
        if name not in self.config.routes[route].candidates:
            raise ValueError(f"{name} is not a candidate for {route}")
        self._forced[route] = name

    def replace_models(self, model: Model):
        """显式的评测模型注入；所有调用角色使用同一个确定性替身。"""
        self.models = dict.fromkeys(self.models, model)

    def _healthy(self, name: str) -> bool:
        from tooling.tool_manager import CircuitState
        breaker = self.breakers[name]
        return breaker.state == CircuitState.CLOSED or (
            breaker.state == CircuitState.OPEN and time.monotonic() - breaker.opened_at >= breaker.recovery_s)

    def pick(self, route: str, tenant: str, *, excluding: frozenset[str] = frozenset(), reason: str | None = None) -> Routed:
        self.reload()
        config = self.config.routes[route]
        # An unlisted tenant explicitly inherits the declared default policy.
        policy = self.config.tenants[tenant] if tenant in self.config.tenants else self.config.tenants["default"]
        candidates = [name for name in config.candidates if name not in excluding
                      and self.config.compliant(self.config.models[name], policy) and self._healthy(name)]
        if route in self._forced:
            candidates = [name for name in candidates if name == self._forced[route]]
        if not candidates:
            raise ModelUnavailableError(route, "no healthy compliant model")
        name = candidates[0]
        selected_reason = "primary" if name == config.candidates[0] else "failover"
        if len(candidates) > 1 and config.latency_slo_s is not None:
            samples = self.latencies[name]
            cutoff = time.monotonic() - 300
            while samples and samples[0][0] < cutoff:
                samples.popleft()
            if samples and percentile([duration for _, duration in samples], .95) > config.latency_slo_s:
                name = candidates[1]
                selected_reason = "latency_shift"
        if route == "agent_write" and self.over_budget(tenant):
            selected_reason = "budget"
        if not self.breakers[name].allow():
            raise ModelUnavailableError(name, "circuit probe busy")
        thinking = config.thinking
        settings = ModelSettings(timeout=config.request_timeout_s,
            max_tokens=3000 if thinking == "disabled" else 16000,
            extra_body={"thinking": {"type": "disabled" if thinking == "disabled" else "enabled"}})
        if thinking == "disabled":
            settings["temperature"] = .2
        else:
            settings["extra_body"]["reasoning_effort"] = thinking
        return Routed(self.models[name], name, route, reason or selected_reason, settings, config.max_rounds, self.breakers[name].generation)

    async def run(self, routed: Routed, operation):
        import asyncio
        started = time.monotonic()
        try:
            result = await operation
        except asyncio.CancelledError:
            self.breakers[routed.name].abandon_probe(routed.generation)
            raise
        except MODEL_ERRORS as exc:
            self.record(routed, time.monotonic() - started, success=not is_model_unavailable(exc))
            raise
        self.record(routed, time.monotonic() - started, success=True)
        return result

    def record(self, routed: Routed, seconds: float, *, success: bool):
        breaker = self.breakers[routed.name]
        self.calls[routed.route] += 1
        before = breaker.state
        if success:
            breaker.record_success(routed.generation)
            now = time.monotonic()
            samples = self.latencies[routed.name]
            samples.append((now, seconds))
            while samples and samples[0][0] < now - 300:
                samples.popleft()
        else:
            breaker.record_failure(routed.generation)
            if self.engine is not None and before != "open" and breaker.state == "open":
                from core.change_log import record_change
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
                from datetime import datetime, timezone
                today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                cost = db.scalar(select(func.coalesce(func.sum(turn_records.c.cost_usd), 0))
                    .where(turn_records.c.tenant == tenant, turn_records.c.created_at >= today))
                result = cost >= quotas["daily_cost_usd"]
        self._cost_cache[tenant] = (now, result)
        return result

    def model_fingerprint(self, name: str) -> str:
        import json
        value = self.config.models[name].model_dump()
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def cost(self, name: str, usage: dict) -> float:
        prices = self.config.models[name].price_per_mtok
        cached = usage.get("cache_read_tokens", 0)
        return ((usage.get("input_tokens", 0) - cached) * prices.input
                + cached * prices.cache_read + usage.get("output_tokens", 0) * prices.output) / 1_000_000
