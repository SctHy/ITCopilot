"""Proactive intake: alerts -> decisions under off / observe / auto, never bypassing approval (ADR 0010)."""
import asyncio
import copy

import pytest
import yaml
from fastapi.testclient import TestClient

import api.main as api
from api import state
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from intake.alerts import AlertRules, ProactiveIntake, load_rules
from tests.conftest import auth
from tests.test_workflow import BOB, Clock
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker

RULES = yaml.safe_load(open("config/alert_rules.yaml", encoding="utf-8"))


def alert(name="ITCopilotToolCircuitOpen", *, fp="fp1", starts="2026-11-03T08:00:00Z", status="firing", **labels):
    return {"status": status, "fingerprint": fp, "startsAt": starts, "generatorURL": "http://prom/graph",
            "labels": {"alertname": name, "severity": "critical", "tool": "knowledge_search", **labels},
            "annotations": {"summary": "工具 knowledge_search 熔断", "description": "5 分钟内持续 open"}}


def webhook(*alerts):
    return {"version": "4", "status": "firing", "alerts": list(alerts)}


@pytest.fixture
def intake(db_url):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="w1", base_delay_s=0)
    rules = copy.deepcopy(RULES)

    async def knowledge(query):
        return [{"citation": "工具熔断处置手册 · c1", "content": f"检索：{query}；先看依赖服务健康，再手动半开探测。", "vector_score": 0.6},
                {"citation": "打印机卡纸 · c9", "content": "无关条目", "vector_score": 0.3}]

    service = ProactiveIntake(store.engine, store, knowledge=knowledge, rules=lambda: AlertRules.from_dict(rules), clock=clock)
    return service, rules, store, worker, clock


def ingest(service, *alerts):
    return asyncio.run(service.ingest(webhook(*alerts)))


def test_observe_mode_records_what_would_be_filed_and_files_nothing(intake):
    service, rules, store, *_ = intake
    [decision] = ingest(service, alert())
    assert decision["decision"] == "would_accept" and decision["rule_id"] == "tool-circuit-open" and decision["approval_id"] is None
    draft = decision["alert"]["draft"]
    assert draft["title"] == "IT-Copilot 工具 knowledge_search 熔断" and draft["priority"] == "P2"
    assert "工具熔断处置手册" in draft["description"] and "检索：knowledge_search 不可用" in draft["description"]
    assert "打印机" not in draft["description"]
    assert store.list(BOB) == []
    assert ingest(service, alert()) == [decision]  # Alertmanager repeats change nothing
    assert service.list()["counts"] == {"would_accept": 1}


def test_auto_mode_files_an_approval_that_a_person_still_decides(intake):
    service, rules, store, worker, _ = intake
    rules["mode"] = "auto"
    [decision] = ingest(service, alert())
    assert decision["decision"] == "accept"
    approval = store.get(decision["approval_id"])
    assert approval["status"] == "pending" and approval["requester"] == "system:proactive" and approval["actions"] == ["create_ticket"]
    asyncio.run(worker.run_until_idle())
    assert store.employee("S0001")["tickets"] == []  # nothing happens without an approver
    store.decide(approval["approval_id"], "approved", BOB, approval["payload_hash"])
    asyncio.run(worker.run_until_idle())
    assert store.employee("S0001")["tickets"][0]["title"] == "IT-Copilot 工具 knowledge_search 熔断"
    assert ingest(service, alert())[0]["approval_id"] == decision["approval_id"]


def test_cooldown_spans_firing_episodes(intake):
    service, rules, store, _, clock = intake
    rules["mode"] = "auto"
    assert ingest(service, alert())[0]["decision"] == "accept"
    again = ingest(service, alert(starts="2026-11-03T09:00:00Z"))[0]
    assert again["decision"] == "ignore" and "240 分钟内已受理" in again["reason"]
    clock.t += 241 * 60
    assert ingest(service, alert(starts="2026-11-03T13:00:00Z"))[0]["decision"] == "accept"
    assert len(store.list(BOB, "pending")) == 1  # the second filing replaced the first, still-pending one (ADR 0007)


def test_gate_ignores_unmatched_ambiguous_disabled_resolved_and_off(intake):
    service, rules, *_ = intake
    assert ingest(service, alert("SomethingElse"))[0]["reason"] == "没有匹配的已启用规则"
    assert ingest(service, alert("VPNGatewayAuthFailureHigh", fp="vpn"))[0]["decision"] == "ignore"  # rule disabled
    rules["rules"].append({**rules["rules"][0], "id": "duplicate-rule"})
    ambiguous = ingest(service, alert(fp="fp2"))[0]
    assert ambiguous["decision"] == "ignore" and "tool-circuit-open" in ambiguous["reason"] and "duplicate-rule" in ambiguous["reason"]
    rules["rules"].pop()
    assert ingest(service, alert(fp="fp3", status="resolved"))[0]["decision"] == "resolved"
    rules["mode"] = "off"
    assert ingest(service, alert(fp="fp4"))[0]["decision"] == "off"


def test_label_lists_match_any_value():
    data = copy.deepcopy(RULES)
    rule = next(r for r in AlertRules.from_dict(data).rules if r.id == "vpn-gateway-auth")
    assert rule.matches({"alertname": "VPNGatewayAuthFailureHigh", "severity": "warning"})
    assert not rule.matches({"alertname": "VPNGatewayAuthFailureHigh", "severity": "info"})
    with pytest.raises(ValueError):
        AlertRules.from_dict({**data, "mode": "yolo"})


def test_shipped_rules_load():
    rules = load_rules()
    assert rules.mode == "observe" and rules.reporter.employee_id == "S0001"


def test_webhook_needs_the_shared_secret_and_decisions_need_an_operator(intake, monkeypatch):
    service, *_ = intake
    monkeypatch.setattr(state, "proactive", service)
    http = TestClient(api.app)
    monkeypatch.delenv("IT_COPILOT_ALERT_TOKEN", raising=False)
    assert http.post("/proactive/alerts", json=webhook(alert())).status_code == 404
    monkeypatch.setenv("IT_COPILOT_ALERT_TOKEN", "s3cret")
    assert http.post("/proactive/alerts", json=webhook(alert())).status_code == 401
    assert http.post("/proactive/alerts", json=webhook(alert()), headers={"Authorization": "Bearer wrong"}).status_code == 401
    body = http.post("/proactive/alerts", json=webhook(alert()), headers={"Authorization": "Bearer s3cret"}).json()
    assert body["decisions"][0]["decision"] == "would_accept"
    assert http.get("/proactive/decisions", headers=auth("alice")).status_code == 403
    listed = http.get("/proactive/decisions?decision=would_accept", headers=auth("erin")).json()
    assert listed["mode"] == "observe" and len(listed["items"]) == 1
