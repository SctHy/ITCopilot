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


def test_skills_are_selected_by_agent_not_by_message(shipped):
    network = shipped.prompt_for("network_software")
    assert "network-software-support" in network and "access-support" not in network      # 只取标给自己的
    everything = shipped.prompt_for(None)                                                  # 通用 Agent
    assert all(name in everything for name in ("network-software-support", "access-support", "endpoint-support", "ticket-service"))


def test_prompt_respects_total_budget(tmp_path):
    for i in range(3):
        (tmp_path / f"s{i}").mkdir()
        (tmp_path / f"s{i}" / "SKILL.md").write_text(f"---\nname: skill-{i}\nkeywords: [VPN]\n---\n" + "规则。" * 2000, encoding="utf-8")
    manager = SkillManager(str(tmp_path), max_prompt_chars=1000)
    manager.load()
    prompt = manager.prompt_for("triage")
    assert len(prompt) <= 1000 + 10 and "skill-0" in prompt and "skill-2" not in prompt


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
    assert manager.prompt_for("triage") == "" and manager.prompt_for(None) == ""


def test_agent_skills_live_in_fixed_system_prompt_not_in_user_turn(shipped):
    from agents.agent_orchestrator import AgentOrchestrator
    from agents.prompting import AGENT_RULES, turn_prompt
    from agents.types import AgentType, Request
    orchestrator = AgentOrchestrator("sk-local", skill_manager=shipped)
    network = orchestrator._pool[AgentType.NETWORK_SOFTWARE][0]
    access = orchestrator._pool[AgentType.ACCESS][0]
    req = Request("VPN 连不上，DNS 解析失败")
    system = network._build_system_prompt(req)
    assert "network-software-support" in system and "network-software-support" not in access._build_system_prompt(req)
    # 规则在前、skill 在后，且声明冲突时以规则为准；用户消息里不再出现 skill。
    assert system.index(AGENT_RULES) < system.index("以规则为准") < system.index("network-software-support")
    assert "network-software-support" not in turn_prompt(req)
    assert system == network._build_system_prompt(Request("打印机卡纸"))
    assert "access-support" in orchestrator._generalist._build_system_prompt(req)
