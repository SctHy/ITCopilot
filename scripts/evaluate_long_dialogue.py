"""长对话评测：同一会话连续 18–24 轮，考察上下文保持、写操作终态与 token 随轮次的增长。

与其他套件的区别：
- 每个会话足够长，必然跨过工作记忆压缩点（COMPRESS_AT=15 条消息，约 7–8 轮），早期事实只能靠摘要/画像带到后面；
- 中途穿插“回忆探针”（问前面说过的事实），按关键词判分；会话结束后按待审批请求判写操作终态（精确匹配，多提/少提都算错）；
- 每轮之后等后台记忆维护（压缩、摘要合并、画像提炼）跑完再发下一轮，模拟真人打字间隔，也让 token 归属确定；
- 用 token 审计逐轮记录 Agent 的输入（命中/未命中缓存）、输出，以及后台记忆维护的调用与费用。

    python scripts/evaluate_long_dialogue.py --output deep_20261112/long/long_base.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import statistics
from datetime import datetime, timezone
import json
import pathlib
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.model_router import ModelRouter
from evaluation.application import application, auth  # noqa: E402
from evaluation.reporting import percentile, write_report  # noqa: E402

# 每轮：(用户消息, 探针关键词)。探针为 None 表示不判分；否则是“任一组全部命中”的关键词组列表（不区分大小写）。
SESSIONS = [
    {
        "id": "L1", "title": "入职第一周：多个流程 + 两次权限申请 + 早期事实回忆",
        "turns": [
            ("你好，我是新来的后端，在 platform 组，用的是公司配的 MacBook Pro", None),
            ("入职第一天 IT 这边要做哪些准备？", None),
            ("VPN 怎么装？", None),
            ("装好了，连的时候报 691", [["691"]]),
            ("改了一下密码好了", None),
            ("我要申请 gitlab 权限，项目 platform，只读就行，先看看代码", None),
            ("对，就这样提交吧", None),
            ("MFA 要怎么绑？", None),
            ("打印机怎么装", None),
            ("会议室投屏没反应怎么办", None),
            ("邮箱里的日历怎么共享给同事", None),
            ("刚才 VPN 报的错误码是多少来着？", [["691"]]),
            ("我前面申请的 gitlab 是什么角色？", [["只读"], ["readonly"]]),
            ("浏览器打开内网页面提示证书不受信任", None),
            ("内网域名有时候解析不了", None),
            ("帮我查一下我名下笔记本的资产号", [["DEV-1001"]]),
            ("这台电脑最近有点慢", None),
            ("再帮我申请 jira 只读，项目 PLAT，看需求用", None),
            ("确认提交", None),
            ("总结一下我今天都申请了哪些权限", [["gitlab", "jira"]]),
            ("我是哪个组的来着？", [["platform"]]),
            ("好的谢谢", None),
        ],
        "expect_writes": [("request_access", {"system": "gitlab", "role": "readonly"}),
                          ("request_access", {"system": "jira", "role": "readonly"})],
    },
    {
        "id": "L2", "title": "蓝屏长链排障：多次补充信息 + 粘贴日志 + 建单 + 回忆细节",
        "turns": [
            ("Windows 电脑开机蓝屏，stop code 是 IRQL_NOT_LESS_OR_EQUAL", None),
            ("前天刚装了扩展坞的驱动", None),
            ("按 F8 进不了安全模式", None),
            ("进去了，卸了驱动重启，又蓝屏了，这次是 PAGE_FAULT_IN_NONPAGED_AREA", None),
            ("内存诊断跑完没报错", None),
            ("事件查看器里是这样的：\nLog Name: System\nSource: Microsoft-Windows-WER-SystemErrorReporting\nEvent ID: 1001\nLevel: Error\n"
             "Description: The computer has rebooted from a bugcheck. The bugcheck was: 0x00000050 (0xffffd40fb2a3c000, 0x0000000000000002, "
             "0xfffff8034c1a2b10, 0x0000000000000002). A dump was saved in: C:\\Windows\\MEMORY.DMP. Report Id: 6f2c1d9e-4b1a-4e2f-9a77-1c0b3e5d8f21.\n"
             "Log Name: System\nSource: disk\nEvent ID: 153\nLevel: Warning\nDescription: The IO operation at logical block address 0x1a2b3c for Disk 0 was retried.", None),
            ("还是不行，帮我建个工单吧，影响我今天交付，优先级高", None),
            ("确认，提交", None),
            ("工单在处理期间我能用什么临时办法办公？", None),
            ("我的数据要不要先备份，怎么备份", None),
            ("OneDrive 同步一直卡住", None),
            ("第一次蓝屏的 stop code 是什么？", [["IRQL_NOT_LESS_OR_EQUAL"]]),
            ("我是装了什么之后才出问题的？", [["扩展坞"], ["dock"]]),
            ("日志里那个磁盘警告的事件 ID 是多少？", [["153"]]),
            ("我刚才提的工单现在什么状态？", [["审批"]]),
            ("另外 outlook 一直弹密码框", None),
            ("wifi 也特别慢", None),
            ("今天这些问题里哪个最紧急？", [["蓝屏"]]),
            ("好的，我先等工单", None),
            ("谢谢", None),
        ],
        "expect_writes": [("create_ticket", {})],
    },
    {
        "id": "L3", "title": "安全事件 + 设备丢失：同一会话两个高风险流程",
        "turns": [
            ("我好像中了钓鱼邮件，点了一个“工资单更新”的链接", None),
            ("还在那个页面输了公司邮箱密码", None),
            ("密码我已经自己改了，还要做什么？", None),
            ("MFA 需要重新绑吗", None),
            ("这个要报给谁？", None),
            ("钓鱼邮件以后怎么识别", None),
            ("可疑邮件要怎么上报", None),
            ("另外一件事，我的笔记本昨天落在高铁上了", None),
            ("就是公司配的那台，工号 1001，帮我远程锁定", None),
            ("确认锁定", None),
            ("锁定之后数据还能恢复吗", None),
            ("我需要临时借一台电脑", None),
            ("今天一开始我是因为什么事来找你的？", [["钓鱼"]]),
            ("我在钓鱼页面输入的是什么？", [["密码"]]),
            ("笔记本落在哪了？", [["高铁"]]),
            ("锁定的申请提交了吗", [["审批"]]),
            ("好，谢谢", None),
            ("对了，钓鱼链接的标题我记得是什么来着", [["工资"]]),
        ],
        "expect_writes": [("lock_device", {"device_id": "DEV-1001"})],
    },
    {
        "id": "L4", "title": "跳跃式短问答 24 轮：只问不办，早期偏好/事实靠摘要带到最后",
        "turns": [
            ("我平时用 iPhone，英文不好，回答尽量用中文", None),
            ("我的工号是 1001，在 platform 组", None),
            ("访客 wifi 怎么连", None),
            ("公司 wifi 呢", None),
            ("Mac 上怎么截屏", None),
            ("磁盘快满了怎么办", None),
            ("代理证书怎么装", None),
            ("软件许可证怎么申请", None),
            ("显示器外接没信号", None),
            ("键盘蓝牙连不上", None),
            ("工单优先级怎么定的", None),
            ("SLA 多久响应", None),
            ("工单要怎么写才处理得快", None),
            ("什么数据不能发外部", None),
            ("调岗权限怎么处理", None),
            ("离职时电脑怎么还", None),
            ("Mac 软件闪退", None),
            ("Chrome 老是崩", None),
            ("会议室麦克风没声音", None),
            ("扫描仪扫不出来", None),
            ("我手机是什么系统来着？", [["iphone"], ["ios"]]),
            ("我的工号多少？", [["1001"]]),
            ("我在哪个组？", [["platform"]]),
            ("好，今天就问这些", None),
        ],
        "expect_writes": [],
    },
]


def _norm(value):
    return str(value or "").casefold()


def probe_passed(reply: str, groups) -> bool:
    text = _norm(reply)
    return any(all(_norm(k) in text for k in group) for group in groups)


def writes_check(approvals, expected):
    actual = [(a["tool_name"], a.get("arguments") or {}) for ap in approvals for a in ap.get("action_requests", [])]
    remaining = list(actual)
    for tool, args in expected:
        hit = next((x for x in remaining if x[0] == tool and all(_norm(x[1].get(k)) == _norm(v) for k, v in args.items())), None)
        if hit is None:
            return False, actual
        remaining.remove(hit)
    return not remaining, actual


def knowledge_docs():
    root = ROOT / "knowledge"
    docs = []
    for path in sorted(root.rglob("*.md")):
        title, _, content = path.read_text(encoding="utf-8").partition("\n")
        if title.startswith("# ") and content.strip():
            docs.append({"title": title[2:].strip(), "content": content.strip(), "source": path.relative_to(root).as_posix()})
    return docs


async def drain_maintenance(state, timeout=60):
    """完成队列才推进下一轮；超时会使报告失去严格串行对照资格。"""
    if state.profile_updater is None:
        return True
    try:
        await asyncio.wait_for(state.profile_updater._queue.join(), timeout)
    except TimeoutError:
        return False
    return True


def source_fingerprint():
    digest = hashlib.sha256()
    for folder in ("api", "agents", "core", "intake", "memory", "tooling", "workflow", "evaluation", "config", "skills", "knowledge"):
        for file in sorted((ROOT / folder).rglob("*")):
            if file.is_file() and "reports" not in file.relative_to(ROOT).parts and file.suffix in {".py", ".json", ".yaml", ".md"}:
                digest.update(str(file.relative_to(ROOT)).encode() + file.read_bytes())
    digest.update(pathlib.Path(__file__).read_bytes())
    return digest.hexdigest()


def durable_turn(state, request_id):
    from sqlalchemy import select
    from core.db import read, turn_records
    from core.tenant_context import tenant_scope
    columns = ("request_id", "agent_spec", "agent_version", "build_sha", "model", "route", "route_reason",
               "model_requests", "cost_usd", "input_tokens", "output_tokens", "cache_read_tokens", "intent_source", "intent_group")
    with tenant_scope("default"), read(state.workflow.engine) as db:
        row = db.execute(select(*(turn_records.c[column] for column in columns)).where(
            turn_records.c.request_id == request_id, turn_records.c.tenant == "default")).mappings().one()
    return dict(row)


async def run(output: str, only: set, *, mode=None, agent_spec=None):
    load_dotenv(ROOT / ".env")
    if mode is not None:
        os.environ["IT_COPILOT_MEMORY_MODE"] = mode
    unknown = only - {session["id"] for session in SESSIONS}
    if unknown:
        raise ValueError(f"unknown sessions: {unknown}")
    from agents.registry import default_spec
    from core.scopes import identity_scope
    from evaluation.token_audit import TokenAudit, summarize
    spec = default_spec()
    selected_spec = (spec.name, spec.version) if agent_spec is None else agent_spec
    before_code = source_fingerprint()
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    sessions = []
    mode_observed = None
    with TokenAudit() as audit:
        for session in SESSIONS:
            if only and session["id"] not in only:
                continue
            session_started = time.monotonic()
            # Same employee id may appear in another session; its grants and pending actions cannot share a database.
            async with application(redis_url=None, documents=knowledge_docs(), maintenance=True,
                                   agent_spec=selected_spec) as (http, app_state):
                mode_observed = app_state.memory.context_mode
                unique = uuid.uuid4().hex
                subject = f"long:{unique}"
                scope = identity_scope("default", subject)
                headers = auth("alice", subject=subject, employee_id="1001", department="engineering")
                # Platform version metadata is a control, never a remembered business fact.
                pin = {"agent_spec": selected_spec[0], "agent_version": selected_spec[1]}
                await app_state.memory._redis.set(app_state.memory._state_key(scope, unique), json.dumps(pin), ex=86400)
                row = {"id": session["id"], "title": session["title"], "turns": []}
                session_start = len(audit.records)
                for index, (message, probe) in enumerate(session["turns"]):
                    before = len(audit.records)
                    turn_started = time.monotonic()
                    resp = await http.post("/chat", json={"message": message, "conv_id": unique,
                        "client_request_id": f"{unique}-{index}"}, headers=headers)
                    latency = time.monotonic() - turn_started
                    body = resp.json() if resp.status_code == 200 else {"response": resp.text, "status": f"http_{resp.status_code}"}
                    agent_part = summarize([r for r in audit.records[before:] if r["role"] in ("agent", "intent")])
                    maintenance_started = time.monotonic()
                    maintenance_done = await drain_maintenance(app_state)
                    maintenance_wait = time.monotonic() - maintenance_started
                    persisted = await app_state.memory.get_state(scope, unique)
                    versions = await asyncio.to_thread(durable_turn, app_state, body["request_id"]) if resp.status_code == 200 else None
                    turn = {"index": index + 1, "user": message, "reply": body.get("response", ""),
                        "status": body.get("status"), "tools_used": body.get("tools_used"),
                        "approval_required": body.get("approval_required"), "awaiting_confirmation": body.get("awaiting_confirmation"),
                        "latency_s": latency, "usage": agent_part, "versions": versions,
                        "maintenance_done": maintenance_done, "maintenance_wait_s": maintenance_wait,
                        "stored_agent_pin": {key: persisted[key] for key in pin if key in persisted},
                        "maintenance": summarize([r for r in audit.records[before:] if r["role"].startswith("memory_")])}
                    if probe:
                        turn["probe"] = probe
                        turn["probe_passed"] = resp.status_code == 200 and body["status"] == "success" and probe_passed(turn["reply"], probe)
                    row["turns"].append(turn)
                    print(f"{mode_observed} {session['id']}.{index + 1:02d} {latency:.2f}s "
                          f"maintenance={'done' if maintenance_done else 'TIMEOUT'}"
                          + (f" probe={'PASS' if turn['probe_passed'] else 'FAIL'}" if probe else ""), flush=True)
                approvals = (await http.get("/approvals", params={"status": "pending"}, headers=headers)).json()["items"]
                drafts = (await http.get("/approvals", params={"status": "draft"}, headers=headers)).json()["items"]
                ok, actual = writes_check(approvals, session["expect_writes"])
                probes = [turn for turn in row["turns"] if "probe" in turn]
                row["profile"] = await app_state.memory._get_profile(scope)
                row.update(writes_passed=ok, writes_actual=actual, writes_expected=session["expect_writes"],
                    draft_ids=[draft["approval_id"] for draft in drafts],
                    probes_passed=sum(turn["probe_passed"] for turn in probes), probes_total=len(probes),
                    usage=summarize(audit.records[session_start:]), wall_s=time.monotonic() - session_started)
                print(f"{mode_observed} {session['id']} writes={ok} probes={row['probes_passed']}/{row['probes_total']}", flush=True)
                sessions.append(row)
                write_report(output.replace(".json", "_partial.json"), {"status": "running", "mode": mode_observed, "sessions": sessions}, echo=False)
        token_audit = audit.summary()
    elapsed = time.monotonic() - started
    turns = [turn for session in sessions for turn in session["turns"]]
    after_code = source_fingerprint()
    serial_ok = all(turn["maintenance_done"] for turn in turns)
    pin_ok = all(turn["stored_agent_pin"] == {"agent_spec": selected_spec[0], "agent_version": selected_spec[1]}
                 for turn in turns)
    report = {"suite": "long-dialogue-memory-ablation", "status": "complete" if serial_ok and pin_ok and before_code == after_code else "invalid",
        "started_at": started_at, "mode": mode_observed, "source_sha256": before_code, "source_sha256_after": after_code,
        "dataset_sha256": hashlib.sha256(json.dumps(SESSIONS, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "agent_spec": selected_spec[0], "agent_version": selected_spec[1],
        "route_settings": ModelRouter().config.model_dump(), "sessions": sessions, "token_audit": token_audit,
        "summary": {"sessions": len(sessions), "turns": len(turns), "repeats": 1,
            "probes_passed": sum(session["probes_passed"] for session in sessions),
            "probes_total": sum(session["probes_total"] for session in sessions),
            "write_sessions_passed": sum(session["writes_passed"] for session in sessions),
            "failed_or_partial_turns": sum(turn["status"] != "success" for turn in turns),
            "maintenance_timeouts": sum(not turn["maintenance_done"] for turn in turns),
            "wall_s": elapsed, "turns_per_s": len(turns) / elapsed,
            "output_tokens_per_wall_s": token_audit["usage"]["output"] / elapsed,
            "chat_p50_s": statistics.median(turn["latency_s"] for turn in turns),
            "chat_p95_s": percentile([turn["latency_s"] for turn in turns], .95),
            "cost_usd_peak": token_audit["cost_usd_peak"], "cost_usd_off_peak": token_audit["cost_usd_off_peak"]},
        "controls": {"session_database_isolation": True, "fixed_spec": list(selected_spec),
            "initial_state": "only platform agent_spec/agent_version metadata, equal for all modes", "maintenance_drained": serial_ok,
            "platform_pin_preserved": pin_ok},
        "limitations": "Four authored sessions, one repeat per mode, fixed keyword probes on successful HTTP turns and the original pending-only write oracle. working still includes conversation summaries; state adds business slots; full adds profile/episodic retrieval. All modes retain current maintenance calls. These are within-conversation probes; no same-owner cross-conversation episode is seeded. First-token latency is not measured by the synchronous /chat path. Costs are configured peak/off-peak estimates. Historical unmatched reports are context, not an A/B baseline."}
    write_report(output, report, echo=False)
    return report


async def run_ablation(output: str, only: set, *, agent_spec=None):
    reports = []
    paths = []
    for mode in ("working", "state", "full"):
        mode_output = str(pathlib.Path(output).with_name(f"memory_ablation_{mode}_20261112.json"))
        reports.append(await run(mode_output, only, mode=mode, agent_spec=agent_spec))
        paths.append(mode_output)
    same_source = len({report["source_sha256"] for report in reports}) == 1
    same_data = len({report["dataset_sha256"] for report in reports}) == 1
    same_runtime = len({json.dumps([report["agent_spec"], report["agent_version"], report["route_settings"]], sort_keys=True)
                        for report in reports}) == 1
    rows = [{"mode": report["mode"], "path": path, **report["summary"],
             "by_session": [{"id": session["id"], "probes_passed": session["probes_passed"],
                             "probes_total": session["probes_total"], "writes_passed": session["writes_passed"]}
                            for session in report["sessions"]]}
            for report, path in zip(reports, paths)]
    for row in rows:
        row["probe_rate"] = row["probes_passed"] / row["probes_total"]
    historical = []
    from evaluation.reporting import REPORTS_DIR
    fixture_path = REPORTS_DIR / "memory_ablation_v2.json"
    if fixture_path.exists():
        fixtures = json.loads(fixture_path.read_text())
        historical.append({"path": str(fixture_path), "kind": "controlled_context_fixtures",
            "model_calls": fixtures["model_calls"], "fixtures_per_mode": fixtures["fixtures_per_mode"],
            "comparison_allowed": False, "reason": "No real model dialogue and different assertions; not an A/B baseline."})
    older_path = REPORTS_DIR / "deep_20261112/long/long_base.json"
    if older_path.exists():
        older = json.loads(older_path.read_text())
        historical.append({"path": str(older_path), "kind": "earlier_long_dialogue",
            "probes_passed": sum(session["probes_passed"] for session in older["sessions"]),
            "probes_total": sum(session["probes_total"] for session in older["sessions"]),
            "comparison_allowed": False, "reason": "Unmatched source/model/version metadata; historical context only."})
    all_complete = all(report["status"] == "complete" for report in reports)
    combined = {"suite": "memory-ablation-matched-long-dialogue", "status": "complete" if all_complete and same_source and same_data and same_runtime else "invalid",
        "mode_order": ["working", "state", "full"], "matched_source": same_source, "matched_data": same_data,
        "matched_runtime": same_runtime, "source_sha256": reports[0]["source_sha256"],
        "dataset_sha256": reports[0]["dataset_sha256"], "results": rows,
        "total_turns": sum(row["turns"] for row in rows), "total_probes": sum(row["probes_total"] for row in rows),
        "within_current_run": {"full_minus_working_probe_rate": rows[2]["probe_rate"] - rows[0]["probe_rate"],
                               "full_minus_state_probe_rate": rows[2]["probe_rate"] - rows[1]["probe_rate"]},
        "historical_context": historical,
        "limitations": "One real-model run of four correlated sessions per mode, no randomized mode order. Provider cache/traffic and stochastic responses may affect latency/cost and probes. No inference of pre/post-governance improvement from unmatched historical reports. Keyword probe success is not semantic correctness; pending-only writes are reported separately. Same immutable AgentSpec and platform-only initial pin control versions. working retains summaries; all modes run the same maintenance. No cross-conversation recall trial is included."}
    write_report(output, combined, echo=False)
    print(json.dumps({"status": combined["status"], "results": rows}, ensure_ascii=False), flush=True)
    return combined


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", default="deep_20261112/long/long_base.json")
    p.add_argument("--sessions", default="", help="comma-separated session ids")
    p.add_argument("--memory-mode", choices=("working", "state", "full"))
    p.add_argument("--ablation", action="store_true", help="run all fixed sessions in working/state/full serially; --output is the aggregate report")
    p.add_argument("--agent-spec")
    p.add_argument("--agent-version")
    args = p.parse_args()
    if bool(args.agent_spec) != bool(args.agent_version):
        p.error("--agent-spec and --agent-version must be used together")
    fixed = (args.agent_spec, args.agent_version) if args.agent_spec else None
    only = set(filter(None, args.sessions.split(",")))
    if args.ablation:
        if args.memory_mode:
            p.error("--ablation cannot be combined with --memory-mode")
        asyncio.run(run_ablation(args.output, only, agent_spec=fixed))
    else:
        asyncio.run(run(args.output, only, mode=args.memory_mode, agent_spec=fixed))


if __name__ == "__main__":
    main()
