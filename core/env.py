"""环境变量读取：取值为空或不是数字时退回默认值，而不是让进程启动失败。"""
import os


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
