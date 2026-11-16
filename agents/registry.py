"""Deployment-owned Agent specifications and deterministic capability selection.

Each name has one deployed version. Updating or reverting the deployment selects the
version; conversations and tasks retain version metadata only for diagnosis.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
import yaml

ROOT = Path(__file__).resolve().parents[1]


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


class Serves(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_groups: frozenset[str]


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    system_prompt: str = Field(min_length=1)
    rules: Literal["agents.prompting.AGENT_RULES"]
    tools: frozenset[str]
    skills: SkillSpec
    routes: AgentRoutes
    serves: Serves
    tenants: frozenset[str]

    @property
    def fingerprint(self):
        data = self.model_dump(mode="json")
        data["tools"] = sorted(data["tools"])
        data["tenants"] = sorted(data["tenants"])
        data["serves"]["intent_groups"] = sorted(data["serves"]["intent_groups"])
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AgentRegistry:
    def __init__(self, directory: Path | str = ROOT / "config/agents", *, tools: set[str], routes: set[str]):
        self.specs: dict[str, AgentSpec] = {}
        for path in sorted(Path(directory).glob("*.yaml")):
            spec = AgentSpec.model_validate(yaml.safe_load(path.read_text()))
            key = spec.name, spec.version
            if spec.name in self.specs:
                raise ValueError(f"Only one deployed version per Agent name: {spec.name}")
            missing = spec.tools - tools - {"*"}
            if any(tool.startswith("mcp:") and re.fullmatch(r"mcp:[^/\s:]+/[^/\s]+", tool) is None for tool in spec.tools):
                raise ValueError(f"Malformed external MCP reference: {key}")
            # External MCP availability can change independently of a published specification.
            if any(not tool.startswith("mcp:") for tool in missing):
                raise ValueError(f"Unknown tools: {missing}")
            if {spec.routes.read, spec.routes.write} - routes:
                raise ValueError(f"Unknown Agent routes: {key}")
            if not (ROOT / spec.skills.root).is_dir():
                raise ValueError(f"Missing skill directory: {spec.skills.root}")
            if not spec.serves.intent_groups or not spec.tenants:
                raise ValueError(f"Agent capability scope must not be empty: {key}")
            self.specs[spec.name] = spec
        if not self.specs:
            raise ValueError("No Agent specifications")
        tenants = {tenant for spec in self.specs.values() for tenant in spec.tenants} - {"*"}
        groups = {group for spec in self.specs.values() for group in spec.serves.intent_groups} - {"*"}
        for tenant in tenants | {"__other_tenant__"}:
            for group in groups | {"__other_group__"}:
                self._resolve(tenant, group)

    def _resolve(self, tenant, group):
        candidates = {}
        for spec in self.specs.values():
            if tenant not in spec.tenants and "*" not in spec.tenants:
                continue
            if group not in spec.serves.intent_groups and "*" not in spec.serves.intent_groups:
                continue
            specificity = (int(group in spec.serves.intent_groups), int(tenant in spec.tenants))
            candidates[spec.name] = specificity
        if not candidates:
            return None
        best = max(candidates.values())
        matches = [name for name, specificity in candidates.items() if specificity == best]
        if len(matches) != 1:
            raise ValueError(f"Ambiguous Agent specifications for {tenant}/{group}: {matches}")
        return matches[0]

    def resolve(self, tenant: str, intent_group: str) -> str:
        name = self._resolve(tenant, intent_group)
        if name is None:
            raise ValueError(f"No Agent serves {tenant}/{intent_group}")
        return name

    def get(self, name: str, version: str) -> AgentSpec:
        spec = self.specs[name]
        if spec.version != version:
            raise KeyError((name, version))
        return spec

    def choose(self, tenant: str, intent_group: str) -> AgentSpec:
        return self.specs[self.resolve(tenant, intent_group)]
