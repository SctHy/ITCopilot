"""Proactive intake: monitoring alerts become incident-ticket requests without anyone asking.

Modelled on DeepSeek dsh-superagent's Eval for inferred candidates:

- A deterministic gate first. The Alertmanager webhook is authenticated with a shared
  bearer secret; an alert is a candidate only when exactly one enabled rule in
  ``config/alert_rules.yaml`` matches its labels. Several matches are ignored as
  ambiguous, and an alert filed within the cooldown is not filed again.
- An accept mode: ``off`` records only, ``observe`` records what *would* be filed (the
  review set before switching), ``auto`` files it.
- "Filing" does not bypass the approval workflow: auto mode creates a ``create_ticket``
  approval under a service identity, so a person still decides every write (ADR 0001).
  The draft carries the alert's labels, annotations and related knowledge-base articles
  as evidence.

Every alert gets exactly one recorded decision per firing episode
(``<source>:<fingerprint>:<startsAt>``), so Alertmanager's repeats change nothing.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, List, Optional

import yaml
from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from core.identity import ROLE_EMPLOYEE, Principal
from monitor import metrics
from workflow import db as schema
from workflow.db import proactive_decisions
from workflow.policy import PolicyViolation
from workflow.store import ApprovalConflict, WorkflowStore

logger = logging.getLogger(__name__)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = "alertmanager"
MODES = ("off", "observe", "auto")
# (query) -> [{"title", "content", "citation"}]
Knowledge = Callable[[str], Awaitable[List[Dict[str, Any]]]]


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    enabled: bool
    match: Dict[str, Any]
    priority: str
    title: str
    knowledge_query: str = ""

    def matches(self, labels: Dict[str, str]) -> bool:
        for key, wanted in self.match.items():
            allowed = wanted if isinstance(wanted, list) else [wanted]
            if labels.get(key) not in [str(v) for v in allowed]:
                return False
        return True


@dataclass(frozen=True)
class AlertRules:
    version: str
    mode: str
    reporter: Principal
    cooldown_s: float
    rules: tuple
    knowledge_min_similarity: float = 0.45

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlertRules":
        mode = str(data.get("mode", "observe"))
        if mode not in MODES:
            raise ValueError(f"alert rules mode must be one of {MODES}")
        reporter = data["reporter"]
        rules = tuple(Rule(id=str(r["id"]), name=str(r.get("name", r["id"])), enabled=bool(r.get("enabled", True)), match=dict(r["match"]),
                           priority=str(r.get("priority", "P3")), title=str(r["title"]), knowledge_query=str(r.get("knowledge_query", "")))
                      for r in data.get("rules", []))
        if len({r.id for r in rules}) != len(rules):
            raise ValueError("alert rule ids must be unique")
        return cls(version=str(data["version"]), mode=mode, cooldown_s=float(data.get("cooldown_minutes", 240)) * 60, rules=rules,
                   knowledge_min_similarity=float(data.get("knowledge_min_similarity", 0.45)),
                   reporter=Principal(subject=str(reporter["subject"]), username=str(reporter["username"]), department=str(reporter["department"]),
                                      roles=frozenset({ROLE_EMPLOYEE}), employee_id=str(reporter["employee_id"])))


@lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int) -> AlertRules:
    return AlertRules.from_dict(yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {})


def load_rules() -> AlertRules:
    path = os.getenv("IT_COPILOT_ALERT_RULES", str(_ROOT / "config/alert_rules.yaml"))
    return _load(path, pathlib.Path(path).stat().st_mtime_ns)


def _render(template: str, values: Dict[str, str]) -> str:
    return template.format_map(defaultdict(str, values)).strip()


def fingerprint(alert: Dict[str, Any]) -> str:
    return str(alert.get("fingerprint") or hashlib.sha256(json.dumps(alert.get("labels") or {}, sort_keys=True).encode()).hexdigest()[:16])


class ProactiveIntake:
    def __init__(self, engine: Engine, workflow: WorkflowStore, *, knowledge: Optional[Knowledge] = None,
                 rules: Callable[[], AlertRules] = load_rules, clock: Callable[[], float] = time.time):
        self.engine = engine
        self.workflow = workflow
        self.knowledge = knowledge
        self.rules = rules
        self.clock = clock

    async def ingest(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """One decision per alert in an Alertmanager webhook payload (version 4)."""
        rules = self.rules()
        return [await self._decide(alert, rules) for alert in payload.get("alerts") or [] if isinstance(alert, dict)]

    async def _decide(self, alert: Dict[str, Any], rules: AlertRules) -> Dict[str, Any]:
        labels = {str(k): str(v) for k, v in (alert.get("labels") or {}).items()}
        fp, status = fingerprint(alert), alert.get("status", "firing")
        decision_id = f"{SOURCE}:{fp}:{alert.get('startsAt', '')}" + (":resolved" if status == "resolved" else "")
        existing = await asyncio.to_thread(self.get, decision_id)
        if existing:
            return existing
        record = {"id": decision_id[:200], "fingerprint": fp, "alertname": labels.get("alertname", "unknown"), "mode": rules.mode,
                  "rules_version": rules.version, "rule_id": None, "approval_id": None,
                  "alert": {"status": status, "labels": labels, "annotations": alert.get("annotations") or {}, "startsAt": alert.get("startsAt"),
                            "generatorURL": alert.get("generatorURL")}}
        matched = [r for r in rules.rules if r.enabled and r.matches(labels)]
        if status == "resolved":
            decision, reason = "resolved", "告警已恢复"
        elif rules.mode == "off":
            decision, reason = "off", "主动受理已关闭（mode=off）"
        elif not matched:
            decision, reason = "ignore", "没有匹配的已启用规则"
        elif len(matched) > 1:
            decision, reason = "ignore", "匹配多条规则，规则有歧义：" + "、".join(r.id for r in matched)
        elif await asyncio.to_thread(self._filed_recently, fp, rules.cooldown_s):
            record["rule_id"] = matched[0].id
            decision, reason = "ignore", f"同一告警 {int(rules.cooldown_s // 60)} 分钟内已受理过"
        else:
            rule = matched[0]
            record["rule_id"] = rule.id
            draft = await self._draft(rule, labels, record["alert"], rules)
            record["alert"]["draft"] = draft
            if rules.mode == "observe":
                decision, reason = "would_accept", f"匹配规则 {rule.id}；观察模式下只记录，不建单"
            else:
                decision, reason = await asyncio.to_thread(self._file, record, draft, rules)
        record.update(decision=decision, reason=reason)
        stored = await asyncio.to_thread(self._store, record)
        if stored["decision"] == decision and stored["id"] == record["id"]:
            metrics.PROACTIVE_DECISIONS.labels(mode=rules.mode, decision=decision).inc()
        return stored

    async def _draft(self, rule: Rule, labels: Dict[str, str], alert: Dict[str, Any], rules: AlertRules) -> Dict[str, str]:
        values = {**labels, **{str(k): str(v) for k, v in alert["annotations"].items()}}
        lines = [
            f"来源：监控告警 {labels.get('alertname', '')}（{labels.get('severity', '未分级')}），开始于 {alert.get('startsAt') or '未知'}",
            f"摘要：{values['summary']}" if values.get("summary") else "",
            f"详情：{values['description']}" if values.get("description") else "",
            "标签：" + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())),
            f"受理规则：{rule.name}（{rule.id}，规则版本 {rules.version}）",
        ]
        if alert.get("generatorURL"):
            lines.append(f"告警链接：{alert['generatorURL']}")
        if self.knowledge and rule.knowledge_query:
            try:
                hits = await self.knowledge(_render(rule.knowledge_query, values))
            except Exception as exc:  # evidence is optional; the ticket is not
                logger.info("knowledge lookup for %s failed: %s", rule.id, exc)
                hits = []
            # Hybrid search always returns top-k; an unrelated article only costs the approver attention.
            hits = [h for h in hits if h.get("vector_score", 1.0) >= rules.knowledge_min_similarity]
            if hits:
                lines.append("相关知识：")
                lines += [f"- {h.get('citation') or h.get('title')}：{(h.get('content') or '')[:120]}" for h in hits[:3]]
        return {"title": _render(rule.title, values)[:200] or rule.name, "description": "\n".join(l for l in lines if l), "priority": rule.priority}

    def _file(self, record: Dict[str, Any], draft: Dict[str, str], rules: AlertRules):
        reporter = rules.reporter
        key = hashlib.sha256(record["id"].encode()).hexdigest()[:24]
        approval_id = f"proactive-{key}:approval"
        try:
            self.workflow.create(approval_id, request_id=f"proactive-{key}", conv_id=f"alert:{record['fingerprint']}", principal=reporter,
                                 action_requests=[{"tool_name": "create_ticket", "arguments": {"employee_id": reporter.employee_id, **draft}}],
                                 summary={"actions": ["create_ticket"], "risk": "medium", "source_message": draft["description"][:500],
                                          "summary": f"监控告警主动建单：{draft['title']}（规则 {record['rule_id']}）"})
        except (PolicyViolation, ApprovalConflict, ValueError) as exc:
            return "ignore", f"建单申请未创建：{exc}"
        record["approval_id"] = approval_id
        return "accept", f"匹配规则 {record['rule_id']}；已提交建单审批"

    def _filed_recently(self, fp: str, cooldown_s: float) -> bool:
        with schema.read(self.engine) as db:
            return db.execute(select(proactive_decisions.c.id).where(
                proactive_decisions.c.source == SOURCE, proactive_decisions.c.fingerprint == fp, proactive_decisions.c.decision == "accept",
                proactive_decisions.c.created_at >= self.clock() - cooldown_s)).first() is not None

    def _store(self, record: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with self.engine.begin() as db:
                db.execute(insert(proactive_decisions).values(source=SOURCE, created_at=self.clock(), **record))
        except IntegrityError:
            pass  # a concurrent delivery of the same alert recorded it first
        return self.get(record["id"])

    def get(self, decision_id: str) -> Optional[Dict[str, Any]]:
        with schema.read(self.engine) as db:
            row = db.execute(select(proactive_decisions).where(proactive_decisions.c.id == decision_id)).mappings().first()
        return self._view(row) if row else None

    @staticmethod
    def _view(row) -> Dict[str, Any]:
        return {k: row[k] for k in ("id", "source", "fingerprint", "alertname", "decision", "mode", "rules_version", "rule_id", "reason",
                                    "approval_id", "alert")} | {"created_at": row["created_at"]}

    def list(self, decision: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
        query = select(proactive_decisions).order_by(proactive_decisions.c.created_at.desc()).limit(limit)
        if decision:
            query = query.where(proactive_decisions.c.decision == decision)
        with schema.read(self.engine) as db:
            rows = db.execute(query).mappings().all()
            counts = dict(db.execute(select(proactive_decisions.c.decision, func.count()).group_by(proactive_decisions.c.decision)).all())
        rules = self.rules()
        return {"mode": rules.mode, "rules_version": rules.version, "counts": counts, "items": [self._view(r) for r in rows]}
