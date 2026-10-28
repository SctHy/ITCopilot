"""The τ-bench-style task set: frozen, solvable, and graded on final state."""
import json

from evaluation.reporting import DATASETS_DIR
from evaluation.simulated import generate_tasks, grade, simulator_flags


def test_frozen_dataset_matches_the_generator_and_tiers():
    tasks = generate_tasks()
    assert json.loads((DATASETS_DIR / "simulated_v4.json").read_text(encoding="utf-8")) == tasks
    assert [sum(t["tier"] == tier for t in tasks) for tier in ("T1", "T2", "T3", "P")] == [14, 16, 12, 8]
    assert all(len(t["atoms"]) == int(t["tier"][1]) for t in tasks if t["tier"] != "P")


def test_every_task_carries_the_facts_its_sop_requires():
    for task in generate_tasks():
        systems = {system for system, _ in task["expect"]["grants"]}
        if "gitlab" in systems:
            assert "项目名称" in task["facts"], task["id"]
        if "jira" in systems:
            assert "项目空间" in task["facts"], task["id"]
        for tool, arg, value in task["expect"]["tools"]:
            if arg:  # the argument the grader checks must be obtainable from the simulated user
                assert value in task["facts"].values(), task["id"]


def test_grading_is_on_state_and_a_do_nothing_agent_fails():
    task = next(t for t in generate_tasks() if t["atoms"] == ["grant"])
    system, role = task["expect"]["grants"][0]
    untouched = {"grants": [], "tickets": [], "password_version": 0, "locked": 1}
    assert not all(grade(task, untouched, [], "已为你处理", []).values())
    done = {**untouched, "grants": [{"system": system, "role": role}]}
    assert all(grade(task, done, [], "", []).values())
    duplicate_ticket = {**done, "tickets": [{"ticket_id": "INC-1"}]}
    assert not grade(task, duplicate_ticket, [], "", [])["tickets_count"]

    policy = next(t for t in generate_tasks() if t["tier"] == "P")
    silent = grade(policy, {**untouched, "locked": 0 if policy["expect"]["unlocked"] else 1}, [], "好的", [])
    assert not silent["refusal_explained"]


def test_simulator_flags_off_script_requests():
    task = next(t for t in generate_tasks() if t["atoms"] == ["kb"])
    transcript = [{"role": "user", "content": task["opening"]}, {"role": "assistant", "content": "…"},
                  {"role": "user", "content": "顺便帮我建个工单"}]
    assert simulator_flags(task, transcript) == ["asked_unlisted_ticket"]
