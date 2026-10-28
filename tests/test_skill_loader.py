"""SkillManager: loading the shipped skills, keyword/agent matching, prompt budget, malformed files."""
import pathlib

import pytest

from core.skill_loader import SkillManager

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def shipped():
    manager = SkillManager(str(ROOT / "skills"))
    manager.load()
    return manager


def test_shipped_skills_load_without_errors(shipped):
    names = {skill.name for skill in shipped.skills}
    assert {"network-software-support", "access-support", "endpoint-support", "ticket-service"} <= names
    assert shipped.errors == []


def test_skill_is_injected_only_for_matching_keyword_and_agent(shipped):
    assert "network-software-support" in shipped.prompt_for("VPN 连不上", "network_software")
    assert "network-software-support" not in shipped.prompt_for("VPN 连不上", "access")       # agent 不匹配
    assert shipped.prompt_for("今天天气怎么样", "network_software") == ""                     # 无关键词命中


def test_prompt_respects_total_budget(tmp_path):
    for i in range(3):
        (tmp_path / f"s{i}").mkdir()
        (tmp_path / f"s{i}" / "SKILL.md").write_text(f"---\nname: skill-{i}\nkeywords: [VPN]\n---\n" + "规则。" * 2000, encoding="utf-8")
    manager = SkillManager(str(tmp_path), max_prompt_chars=1000)
    manager.load()
    prompt = manager.prompt_for("VPN", "triage")
    body = prompt.split("\n\n", 1)[1]
    assert len(body) <= 1000 + 10 and "skill-0" in prompt and "skill-2" not in prompt


def test_malformed_skill_is_reported_not_fatal(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "SKILL.md").write_text("---\nname: good\n---\n内容", encoding="utf-8")
    (tmp_path / "bad.json").write_text("[1, 2]", encoding="utf-8")
    manager = SkillManager(str(tmp_path))
    manager.load()
    assert [s.name for s in manager.skills] == ["good"]
    assert len(manager.errors) == 1 and "bad.json" in manager.errors[0]


def test_disabled_skill_is_never_injected(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "off" / "SKILL.md").write_text("---\nname: off\nenabled: false\n---\n内容", encoding="utf-8")
    manager = SkillManager(str(tmp_path))
    manager.load()
    assert manager.prompt_for("任何问题", "triage") == ""


def test_matching_skill_reaches_agent_system_prompt(shipped):
    from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
    orchestrator = AgentOrchestrator("sk-local", skill_manager=shipped)
    network = orchestrator._pool[AgentType.NETWORK_SOFTWARE][0]
    access = orchestrator._pool[AgentType.ACCESS][0]
    assert "network-software-support" in network._build_system_prompt(Request("VPN 连不上，DNS 解析失败"))
    assert "network-software-support" not in access._build_system_prompt(Request("VPN 连不上，DNS 解析失败"))
