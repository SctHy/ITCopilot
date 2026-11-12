"""环境变量读取：取值为空或不是数字时退回默认值，而不是让进程启动失败。"""
import math
import os


def env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def multi_tenant_enabled() -> bool:
    """IT_COPILOT_MULTI_TENANT (ADR 0022); here so core.identity can read it without importing core.tenancy."""
    return os.getenv("IT_COPILOT_MULTI_TENANT", "false").strip().lower() in {"1", "true", "yes", "on"}
