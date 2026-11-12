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


def test_prompt_contains_every_enabled_skill(shipped):
    everything = shipped.prompt()
    assert all(name in everything for name in ("network-software-support", "access-support", "endpoint-support", "ticket-service"))


def test_prompt_respects_total_budget(tmp_path):
    for i in range(3):
        (tmp_path / f"s{i}").mkdir()
        (tmp_path / f"s{i}" / "SKILL.md").write_text(f"---\nname: skill-{i}\nkeywords: [VPN]\n---\n" + "规则。" * 2000, encoding="utf-8")
    manager = SkillManager(str(tmp_path), max_prompt_chars=1000)
    manager.load()
    prompt = manager.prompt()
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
    assert manager.prompt() == ""


def test_agent_skills_live_in_fixed_system_prompt_not_in_user_turn(shipped):
    from agents.agent_orchestrator import AgentOrchestrator
    from agents.prompting import AGENT_RULES, turn_prompt
    from agents.types import Request
    agent = AgentOrchestrator("sk-local", skill_manager=shipped).agent
    req = Request("VPN 连不上，DNS 解析失败")
    system = agent._build_system_prompt(req)
    assert "network-software-support" in system and "access-support" in system
    # 规则在前、skill 在后，且声明冲突时以规则为准；用户消息里不再出现 skill。
    assert system.index(AGENT_RULES) < system.index("以规则为准") < system.index("network-software-support")
    assert "network-software-support" not in turn_prompt(req)
    assert system == agent._build_system_prompt(Request("打印机卡纸"))


def test_progressive_disclosure_puts_index_in_system_and_selected_bodies_in_the_turn(monkeypatch):
    """渐进式披露：system 只放固定目录（可命中前缀缓存），本轮相关技能正文进本轮消息；auto 在技能少时用 full。"""
    from pathlib import Path
    from core.skill_loader import SkillManager
    from agents.prompting import turn_prompt
    from agents.types import Request
    manager = SkillManager(str(Path(__file__).resolve().parents[1] / "skills"))
    manager.load()
    assert manager.disclosure() == "full"                       # 默认 auto，当前技能总量远低于阈值
    monkeypatch.setenv("IT_COPILOT_SKILL_DISCLOSURE", "progressive")
    assert manager.disclosure() == "progressive"
    assert [s.name for s in manager.select("笔记本黑屏，帮我建单", "endpoint", {"create_ticket"})] == ["endpoint-support", "ticket-service"]
    assert manager.select("谢谢", "general") == []
    text = manager.selected_prompt("VPN 报 691", "network")
    assert "network-software-support" in text and "ticket-service" not in text
    assert "[业务规范]" in turn_prompt(Request("VPN 报 691", skill_text=text))
