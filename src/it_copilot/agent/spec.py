"""Configuration for the deployed service agent."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

ROOT = Path(__file__).resolve().parents[3]


def default_spec():
    return AgentSpec.model_validate(yaml.safe_load((ROOT / "config/agents/it_copilot.yaml").read_text()))


class SkillSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    root: str
    disclosure: Literal["full", "progressive"]


class AgentRoutes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    read: str
    write: str


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    system_prompt: str = Field(min_length=1)
    rules: Literal["it_copilot.agent.prompting.AGENT_RULES"]
    tools: frozenset[str]
    skills: SkillSpec
    routes: AgentRoutes
