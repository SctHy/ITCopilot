"""Isolated DevSuite A/B/C trials. Only user turns and delivered events reach the policies."""

import asyncio
import hashlib
import json
import logging
import os
import platform
import random
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import insert, select, update

from servicedesk_agent.agent.llm import build_model, close_model
from servicedesk_agent.agent.skill_loader import SkillManager
from servicedesk_agent.eval.devsuite_driver import Session, fixture_model
from servicedesk_agent.eval.ledger import ROOT, EnvMetadata
from servicedesk_agent.infra import db as dbs
from servicedesk_agent.infra.identity import Principal
from servicedesk_agent.infra.tenant_context import tenant_scope
from servicedesk_agent.workflow.adapters import AdapterRegistry
from servicedesk_agent.workflow.adapters.base import AmbiguousError, TransientError
from servicedesk_agent.workflow.adapters.local import LocalDirectoryAdapter
from servicedesk_agent.workflow.approval import approval_from_traces
from servicedesk_agent.workflow.store import ApprovalConflict, WorkflowStore
from servicedesk_agent.workflow.worker import ExecutionWorker

DATA = ROOT / "evaluation/datasets/devsuite_abc_v1.json"
NOW = 1800000000.0
REVIEWERS = [
    Principal("dev:" + n, n, "engineering", frozenset({"approver"}), emp)
    for n, emp in [("carol", "2002"), ("bob", "2001")]
]


class FaultAdapter(LocalDirectoryAdapter):
    def __init__(self, store, fault, journal):
        super().__init__(store.directory)
        self.fault, self.journal = fault, journal
        self.fired = False

    async def apply(self, action_id, action):
        name = action["tool_name"]
        self.journal.append(["apply", action_id, name])
        if self.fault == "step2_fail_once" and name == "add_access_group" and not self.fired:
            self.fired = True
            self.journal.append(["fault", action_id, self.fault])
            raise TransientError("注入组写入暂时失败")
        result = await super().apply(action_id, action)
        if (
            self.fault == "after_commit_before_response"
            and name == "assign_license"
            and not self.fired
        ):
            self.fired = True
            self.journal.append(["fault", action_id, self.fault])
            raise AmbiguousError("注入提交后响应丢失")
        return result

    async def observe(self, action_id, action):
        self.journal.append(["observe", action_id, action["tool_name"]])
        return await super().observe(action_id, action)


def seed(store, case):
    state = case["initial_state"]
    emp = case["actor"]["employee_id"]
    with store.engine.begin() as db:
        db.execute(
            update(dbs.directory_employees)
            .where(dbs.directory_employees.c.id == emp)
            .values(active=int(state["account_active"]), locked=int(state["locked"]))
        )
        db.execute(
            update(dbs.directory_service_health).values(
                status=state["service_health"],
                message="正常" if state["service_health"] == "healthy" else "生产集群不可用",
            )
        )
        db.execute(
            update(dbs.directory_software_inventory).values(
                available_seats=state["available_seats"], total_seats=state["total_seats"]
            )
        )
        values = dict(
            tenant=case["actor"]["tenant"],
            employee_id=emp,
            software="devsuite",
            operation_id="seed",
        )
        if state["license_scope"]:
            db.execute(
                insert(dbs.directory_licenses).values(
                    **values,
                    scope=state["license_scope"],
                    expires_at=NOW + (-86400 if state["expired"] else 86400),
                    active=1,
                )
            )
        if state["group_member"]:
            db.execute(
                insert(dbs.directory_access_groups).values(
                    **values, group_name=state["license_scope"]
                )
            )


TABLES = (
    dbs.directory_employees,
    dbs.directory_licenses,
    dbs.directory_access_groups,
    dbs.directory_software_inventory,
    dbs.directory_service_health,
    dbs.directory_tickets,
    dbs.directory_effects,
    dbs.approvals,
    dbs.approval_decisions,
    dbs.actions,
    dbs.audit,
)


def snapshot(store):
    with store.engine.connect() as db:
        return {
            t.name: [dict(row) for row in db.execute(select(t).order_by(*t.primary_key)).mappings()]
            for t in TABLES
        }


async def conversation(store, case, session, row, adapter):
    drafts = []
    events = case["events"]
    actor = session.actor

    async def turn(message, notice="", approvals=()):
        start = len(row["tools"])
        await session.respond(message, notice, approvals)
        proposal = approval_from_traces(row["tools"][start:], message)
        if proposal:
            name = f"draft-{len(drafts) + 1}"
            body = store.create(
                name,
                request_id=name,
                conv_id=row["run_id"],
                principal=actor,
                ask_requester=True,
                action_requests=proposal["action_requests"],
            )
            drafts.append(body)
            row["turns"][-1]["created"] = name
            return body
        return None

    old = None
    for idx, message in enumerate(case["turns"]):
        if idx and "revise" in events and drafts:
            old = drafts[-1]
            store.withdraw(old["approval_id"], actor, "申请人修改范围")
        body = await turn(message)
        if body and body["status"] == "draft" and events:
            store.confirm(body["approval_id"], actor, body["payload_hash"])
    if not drafts or not events:
        return
    last = drafts[-1]
    name, digest = last["approval_id"], last["payload_hash"]
    if "exhaust" in events:
        with store.engine.begin() as db:
            db.execute(update(dbs.directory_software_inventory).values(available_seats=0))
    if "stale_vote" in events and old and last != old:
        try:
            store.decide(name, "approved", REVIEWERS[0], old["payload_hash"])
        except ApprovalConflict:
            row["events"].append("stale_hash_blocked")
    if "reject" in events:
        store.decide(name, "rejected", REVIEWERS[0], digest)
    if "approve" in events or "one_vote" in events:
        votes = 1 if "one_vote" in events else last["approvals_required"]
        for reviewer in REVIEWERS[:votes]:
            store.decide(name, "approved", reviewer, digest)
        if "duplicate" in events:
            store.decide(name, "approved", reviewer, digest)
            row["events"].append("duplicate_delivered")
    worker = ExecutionWorker(store, AdapterRegistry(adapter), base_delay_s=0)
    await worker.run_until_idle()
    current = store.get(name)
    notice = f"{current['status']} / {current['execution']}"
    await turn(
        f"审批卡片当前状态 {notice}。请核实实际结果并简短告诉我现在的状态，不要再提交。",
        notice,
        [current],
    )


async def run_trial(case, arm, repeat, model, model_name, skills, template):
    run_id = f"{case['case_id']}-{arm}-{repeat}"
    row = dict(
        run_id=run_id,
        case_id=case["case_id"],
        split=case["split"],
        arm=arm,
        repeat=repeat,
        tools=[],
        turns=[],
        events=[],
        adapter=[],
        api_calls=[],
        errors=[],
        fault=case["fault"],
    )
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "db"
        shutil.copyfile(template, path)
        store = WorkflowStore(f"sqlite:///{path}", clock=lambda: NOW, run_migrations=False)
        try:
            actor_data = dict(case["actor"], username="alice")
            actor_data["roles"] = frozenset(actor_data["roles"])
            actor = Principal(**actor_data)
            with tenant_scope(actor.tenant):
                seed(store, case)
                before = snapshot(store)
                session = Session(arm, store, actor, row, case["budget"], model, model_name, skills)
                adapter = FaultAdapter(store, case["fault"], row["adapter"])
                start = time.monotonic()
                try:
                    async with asyncio.timeout(case["budget"]["max_active_seconds"]):
                        await conversation(store, case, session, row, adapter)
                except Exception as exc:
                    import traceback

                    row["errors"].append(
                        type(exc).__name__
                        + "@"
                        + "/".join(
                            f.name + "." + str(f.lineno)
                            for f in traceback.extract_tb(exc.__traceback__)[-4:]
                        )
                    )
                row["agent_active_ms"] = round(1000 * (time.monotonic() - start), 2)
                row["fault_fired"] = adapter.fired
                after = snapshot(store)
                row["db"] = {
                    key: [v for v in val if v not in before[key]]
                    for key, val in after.items()
                    if key in ("directory_tickets", "directory_employees")
                }
                row["db"].update({k: v for k, v in after.items() if k not in row["db"]})
                row["cost_usd"] = sum(session.router.cost(model_name, c) for c in row["api_calls"])
                row["tokens"] = sum(
                    c.get("input_tokens", 0) + c.get("output_tokens", 0) for c in row["api_calls"]
                )
        finally:
            await store.aclose()
    return row


async def experiment(driver, repeats, concurrency, out):
    cases = json.loads(DATA.read_text())
    config = dotenv_values(ROOT / ".env") if driver == "live" else {}
    model_name = (
        config.get("OPENAI_MODEL", os.getenv("OPENAI_MODEL", "deepseek-flash"))
        if driver == "live"
        else "deepseek-flash"
    )
    if driver == "live" and not (config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")):
        raise ValueError("live driver requires OPENAI_API_KEY")
    model = (
        build_model(
            config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY"),
            config.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL"),
            model_name,
        )
        if driver == "live"
        else fixture_model()
    )
    skills = SkillManager(str(ROOT / "skills/it_helpdesk"))
    skills.load()
    env = asdict(EnvMetadata.capture(driver, repeats, [DATA]))
    os.environ.setdefault("SERVICEDESK_AGENT_BUILD_SHA", env["git_sha"])
    env.update(
        model=model_name if driver == "live" else "FunctionModel",
        driver=driver,
        concurrency=concurrency,
        database="SQLite",
        python=platform.python_version(),
        dataset_sha256=hashlib.sha256(DATA.read_bytes()).hexdigest(),
        skill_sha256=skills.fingerprint,
        source_sha256={
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder, pattern in (
                ("src/servicedesk_agent", "*.py"),
                ("config", "*.yaml"),
                ("migrations", "*.py"),
            )
            for p in sorted((ROOT / folder).rglob(pattern))
        },
        shuffle_seed=731,
        clock_epoch=NOW,
    )
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    sem = asyncio.Semaphore(concurrency)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "template"
            dbs.migrate(f"sqlite:///{template}")

            async def trial(case, arm, repeat):
                async with sem:
                    row = await run_trial(case, arm, repeat, model, model_name, skills, template)

                    rows.append(row)
                    if len(rows) % 12 == 0:
                        print(f"{len(rows)} / {len(jobs)}", flush=True)

            jobs = [
                (case, arm, r) for r in range(1, repeats + 1) for case in cases for arm in "ABC"
            ]
            random.Random(731).shuffle(jobs)
            await asyncio.gather(*(trial(*job) for job in jobs))
    finally:
        if driver == "live":
            await close_model(model)
    rows.sort(key=lambda r: (r["case_id"], r["repeat"], r["arm"]))
    from servicedesk_agent.eval.devsuite_judge import score, summarize, markdown

    by_id = {c["case_id"]: c for c in cases}
    for row in rows:
        row.update(score(by_id[row["case_id"]], row))
    report = dict(env=env, runs=rows, **summarize(rows))
    data = json.dumps(report, ensure_ascii=False, indent=1)
    for key, secret in config.items():
        if (
            secret
            and ("API_KEY" in key or "PASSWORD" in key)
            and len(secret) > 7
            and secret in data
        ):
            raise ValueError("credential found in output")
    (out / "devsuite_abc_results.json").write_text(data + "\n")
    (out / "devsuite_abc_report.md").write_text(markdown(report))
    return report


def command(args):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    out = args.output_dir or ROOT / "evaluation/reports" / (
        "offline" if args.driver == "offline" else ""
    )
    asyncio.run(experiment(args.driver, args.repeats, args.concurrency, out))
    return 0
