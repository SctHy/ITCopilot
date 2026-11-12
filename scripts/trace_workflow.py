"""完整工作流追踪：跑一段真实对话，逐轮列出每个环节的调用与 token 消耗。

覆盖的环节（按一轮内的发生顺序）：
  memory    读取记忆（工作记忆条数 / 会话摘要 / 情景记忆命中 / 用户画像）
  intent    意图识别（Jev 调用 + 低置信时转 LLM）
  rag       知识库检索（与意图识别并行的预取，以及 Agent 主动调用的 search_knowledge_base）
  skill     业务规范披露（full：全部在 system；progressive：本轮选中的条目随本轮消息附上）
  llm       每一次模型请求：角色、实际 token（命中/未命中缓存、输出）、请求里各组成部分的字符数与折算 token
  tool      Agent 的每次工具调用：参数、成功与否、耗时、返回大小
  memory_*  回合结束后的后台记忆维护（压缩摘要、画像提炼）

各组成部分的 token 是按字符占比把该次请求的真实 prompt_tokens 分摊出来的估算值；每次请求的总量、
缓存命中与输出都取自模型服务返回的 usage，是真实值。

    python scripts/trace_workflow.py                       # 默认三轮：排障（RAG）→ 建单（写操作）→ 查资产（读工具）
    python scripts/trace_workflow.py -m "VPN 报 691 怎么办"  # 自定义消息，可重复 -m 组成多轮
    python scripts/trace_workflow.py --output workflow_trace.json

报告写入 evaluation/reports/<output>，包含每次模型请求的完整 messages，可直接查看实际发给模型的 prompt。
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import functools
import json
import pathlib
import sys
import time
import uuid
from collections import defaultdict
from typing import Any, Dict, List

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

DEFAULT_TURNS = [
    "VPN 连不上，报错 691，怎么办？",
    "改了密码还是连不上，帮我提个工单吧",
    "顺便帮我查一下我名下笔记本的资产号",
]

_turn_start = contextvars.ContextVar("turn_start", default=0.0)
_in_tool = contextvars.ContextVar("in_tool", default="")


class Tracer:
    def __init__(self):
        self.events: List[Dict[str, Any]] = []
        self.turn = 0
        self.t0 = time.monotonic()
        self.system_split: Dict[str, int] = {}

    def add(self, kind: str, **detail):
        event = {"turn": self.turn, "t_ms": round((time.monotonic() - self.t0) * 1000), "kind": kind, **detail}
        self.events.append(event)
        return event


def _wrap_async(owner, name, after, on_error=None):
    original = getattr(owner, name)

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        started = time.monotonic()
        try:
            result = await original(*args, **kwargs)
        except BaseException as exc:
            if on_error:
                on_error(exc, round((time.monotonic() - started) * 1000, 1))
            raise
        after(args, kwargs, result, round((time.monotonic() - started) * 1000, 1))
        return result

    setattr(owner, name, wrapper)
    return lambda: setattr(owner, name, original)


def instrument(tracer: Tracer, audit) -> List:
    """在真实实现外面包一层只记录、不改行为的探针；返回还原函数列表。"""
    from openai.resources.chat.completions import AsyncCompletions

    from agents.base_agent import BaseAgent
    from agents.prompting import AGENT_RULES
    from core.embedding import Embedder
    from core.jev import JevClient
    from core.skill_loader import SkillManager
    from memory.conversation_memory import MemoryManager
    from tooling.tool_manager import MCPToolManager

    restores = []

    # --- 模型请求：TokenAudit 已记录组成与 usage，这里补上序号、时间、完整 messages 与响应里的工具调用。
    audited = AsyncCompletions.create

    async def create(client_self, *args, **kwargs):
        started = time.monotonic()
        response = await audited(client_self, *args, **kwargs)
        record = audit.records[-1] if audit.records and not kwargs.get("stream") else {"role": "stream", "chars": {}, "usage": {}}
        choice = (getattr(response, "choices", None) or [None])[0]
        message = getattr(choice, "message", None)
        calls = [c.function.name for c in (getattr(message, "tool_calls", None) or [])]
        tracer.add("llm", role=record["role"], ms=round((time.monotonic() - started) * 1000), usage=record["usage"],
                   chars=record["chars"], tools_offered=[t.get("function", {}).get("name") for t in kwargs.get("tools") or []],
                   tool_calls=calls, finish=getattr(choice, "finish_reason", None),
                   output_preview=(getattr(message, "content", None) or "")[:160],
                   system_split=dict(tracer.system_split) if record["role"] == "agent" else None,
                   messages=kwargs.get("messages"))
        return response

    AsyncCompletions.create = create
    restores.append(lambda: setattr(AsyncCompletions, "create", audited))

    # --- 意图识别（Jev）
    restores.append(_wrap_async(JevClient, "ask", lambda a, k, r, ms: tracer.add(
        "intent.jev", ms=ms, model=r.model, usage=r.usage,
        answers={q: ({"choice": "p", "p": round(float(v["noul"]), 3)} if "noul" in v else
                     {"choice": v.get("choice"), "p": round(max((v.get("probabilities") or {"": v.get("confidence", 0)}).values()), 3)})
                 for q, v in (r.answers or {}).items()}),
        on_error=lambda exc, ms: tracer.add("intent.jev", ms=ms, error=f"{type(exc).__name__}: {exc}"[:160], usage={}, answers={})))

    # --- 记忆读取
    def on_memory(a, k, ctx, ms):
        tracer.add("memory.read", ms=ms, recent_messages=len(ctx.recent_messages), summary_chars=len(ctx.summary or ""),
                   episodic_hits=len(ctx.relevant_history), profile_keys=sorted((ctx.user_profile or {}).keys()))
    restores.append(_wrap_async(MemoryManager, "get_context", on_memory))

    # --- RAG：预取与 Agent 主动检索走同一个入口，用 contextvar 区分
    def on_search(a, k, r, ms):
        query = a[2] if len(a) > 2 else k.get("query")
        hits = r.data if r.success and isinstance(r.data, list) else []
        tracer.add("rag.search", source=_in_tool.get() or "prefetch", ms=ms, query=query, top_k=k.get("top_k", a[3] if len(a) > 3 else None),
                   success=r.success, error=r.error or None, cached=getattr(r, "cached", None),
                   hits=[{"title": h.get("title"), "score": h.get("score"), "vec": h.get("vector_score"), "bm25": h.get("bm25_score"),
                          "chars": len(h.get("content") or "")} for h in hits if isinstance(h, dict)])
    restores.append(_wrap_async(MCPToolManager, "search", on_search))

    original_embed = Embedder.embed_query

    def embed_query(self, text):
        started = time.monotonic()
        vector = original_embed(self, text)
        tracer.add("rag.embed", ms=round((time.monotonic() - started) * 1000, 1), model=self.model_name, chars=len(text))
        return vector
    Embedder.embed_query = embed_query
    restores.append(lambda: setattr(Embedder, "embed_query", original_embed))

    # --- Skill：记录披露方式；progressive 时记录本轮选中的条目
    original_select = SkillManager.select

    def select(self, message="", intent_group=None, writes=()):
        chosen = original_select(self, message, intent_group, writes)
        tracer.add("skill.select", intent_group=intent_group, writes=sorted(writes or ()), selected=[s.name for s in chosen],
                   chars=sum(len(s.to_prompt_block()) for s in chosen))
        return chosen
    SkillManager.select = select
    restores.append(lambda: setattr(SkillManager, "select", original_select))

    original_system = BaseAgent._build_system_prompt

    def build_system(self, req):
        prompt = original_system(self, req)
        base = len(f"{self.system_prompt}\n{AGENT_RULES}")
        tracer.system_split = {"rules": base, "skills": len(prompt) - base}
        return prompt
    BaseAgent._build_system_prompt = build_system
    restores.append(lambda: setattr(BaseAgent, "_build_system_prompt", original_system))

    # --- 工具调用
    original_invoke = BaseAgent._invoke_tool

    async def invoke(self, deps, spec, args, tool_id):
        token = _in_tool.set(spec.name)
        try:
            started = time.monotonic()
            result = await original_invoke(self, deps, spec, args, tool_id)
        finally:
            _in_tool.reset(token)
        trace = deps.traces[-1] if deps.traces else {}
        tracer.add("tool", name=spec.name, args=args, success=trace.get("success"), outcome=trace.get("outcome"),
                   error=trace.get("error") or None, ms=round((time.monotonic() - started) * 1000, 1),
                   retry_safe=spec.retry_safe, result_chars=len(json.dumps(result, ensure_ascii=False, default=str)),
                   requires_approval=isinstance(result, dict) and bool(result.get("requires_approval")))
        return result
    BaseAgent._invoke_tool = invoke
    restores.append(lambda: setattr(BaseAgent, "_invoke_tool", original_invoke))
    return restores


# ---------------------------------------------------------------- 汇总

def apportion(event: Dict[str, Any]) -> Dict[str, float]:
    """把一次请求的真实 prompt_tokens 按字符占比分摊到各组成部分（估算）。"""
    chars = dict(event.get("chars") or {})
    prompt = (event.get("usage") or {}).get("prompt", 0)
    parts: Dict[str, int] = {}
    split = event.get("system_split")
    if split and chars.get("system"):
        parts["system·规则"] = min(split["rules"], chars["system"])
        if split["skills"] > 0:
            parts["system·skill(全量)"] = chars["system"] - parts["system·规则"]
    elif chars.get("system"):
        parts["system"] = chars["system"]
    parts["tools定义"] = chars.get("tools", 0)
    parts["history多轮"] = chars.get("history", 0)
    sections = {k[5:]: v for k, v in chars.items() if k.startswith("turn:")}
    if sections:
        parts.update({f"本轮·{k}": v for k, v in sections.items()})
    else:
        parts["本轮"] = chars.get("turn", 0)
    parts["本轮内工具往返"] = chars.get("in_turn", 0)
    total = sum(parts.values()) or 1
    return {k: round(prompt * v / total) for k, v in parts.items() if v}


def print_turn(turn: int, message: str, body: Dict[str, Any], events: List[Dict[str, Any]], latency: float):
    from evaluation.token_audit import cost
    print(f"\n{'═' * 100}\n第 {turn} 轮  用户：{message}\n{'─' * 100}")
    start = events[0]["t_ms"] if events else 0
    for e in events:
        t = f"+{e['t_ms'] - start:>6}ms"
        k = e["kind"]
        if k == "memory.read":
            line = f"recent={e['recent_messages']} 摘要={e['summary_chars']}字 情景命中={e['episodic_hits']} 画像={e['profile_keys'] or '-'}"
        elif k == "intent.jev" and e.get("error"):
            line = f"{e['ms']}ms 失败 → 转 LLM 意图识别：{e['error']}"
        elif k == "intent.jev":
            line = f"{e['ms']}ms usage={e['usage']} " + " ".join(f"{q}={v['p']}" if v["choice"] == "p" else f"{q}={v['choice']}({v['p']})" for q, v in e["answers"].items())
        elif k == "rag.embed":
            line = f"{e['ms']}ms {e['model']} query {e['chars']}字"
        elif k == "rag.search":
            hits = "; ".join(f"{h['title']}({h['score']})" for h in e["hits"][:5]) or (e["error"] or "无结果")
            line = f"[{e['source']}] {e['ms']}ms q={e['query']!r} top_k={e['top_k']} → {hits}"
        elif k == "skill.select":
            line = f"intent_group={e['intent_group']} writes={e['writes']} → {e['selected'] or '无'} ({e['chars']}字)"
        elif k == "tool":
            line = (f"{e['name']}({json.dumps(e['args'], ensure_ascii=False)}) → {'OK' if e['success'] else 'FAIL'} "
                    f"{e['ms']}ms 返回{e['result_chars']}字" + (" [待审批]" if e["requires_approval"] else "")
                    + (f" err={e['error']}" if e["error"] else ""))
        elif k == "llm":
            u = e["usage"]
            line = (f"{e['role']:<14} {e['ms']}ms  in={u.get('prompt', 0)} (hit {u.get('hit', 0)} / miss {u.get('miss', 0)}) "
                    f"out={u.get('output', 0)}  ${cost(u):.6f}  → "
                    + (f"调用工具 {e['tool_calls']}" if e["tool_calls"] else f"回答 finish={e['finish']}"))
            print(f"  {t}  {k:<12} {line}")
            shares = apportion(e)
            if shares:
                print(" " * 26 + "输入构成≈ " + "  ".join(f"{name} {v}" for name, v in shares.items()))
            continue
        else:
            line = json.dumps({x: y for x, y in e.items() if x not in ("turn", "t_ms", "kind")}, ensure_ascii=False)[:200]
        print(f"  {t}  {k:<12} {line}")
    print(f"{'─' * 100}\n  意图={body.get('intent')}/{body.get('intent_group')} 工具={body.get('tools_used')} "
          f"待审批={body.get('approval_required')} 阶段耗时={body.get('stage_ms')} 总耗时={latency:.2f}s")
    reply = (body.get("response") or "").replace("\n", " ")
    print(f"  回复：{reply[:300]}{'…' if len(reply) > 300 else ''}")


def summarize_run(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    from evaluation.token_audit import cost
    llm = [e for e in events if e["kind"] == "llm"]
    by_role: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_part: Dict[str, int] = defaultdict(int)
    for e in llm:
        row = by_role[e["role"]]
        row["calls"] += 1
        for key in ("prompt", "hit", "miss", "output", "reasoning"):
            row[key] += e["usage"].get(key, 0)
        for part, tokens in apportion(e).items():
            by_part[f"{e['role']}:{part}"] += tokens
    jev = [e for e in events if e["kind"] == "intent.jev"]
    total = {k: sum(r[k] for r in by_role.values()) for k in ("prompt", "hit", "miss", "output", "reasoning")}
    return {
        "llm_calls": len(llm), "usage": total, "cost_usd_off_peak": round(cost(total), 6), "cost_usd_peak": round(cost(total, "peak"), 6),
        "by_role": {r: {**v, "cost_usd": round(cost(v), 6)} for r, v in by_role.items()},
        "input_tokens_by_part_estimated": dict(sorted(by_part.items(), key=lambda x: -x[1])),
        "jev": {"calls": len(jev), "usage": {k: sum(int(e["usage"].get(k) or 0) for e in jev) for k in {k for e in jev for k in e["usage"]}}},
        "tools": {name: sum(1 for e in events if e["kind"] == "tool" and e["name"] == name) for name in {e["name"] for e in events if e["kind"] == "tool"}},
        "rag": {"prefetch": sum(1 for e in events if e["kind"] == "rag.search" and e["source"] == "prefetch"),
                "agent_search": sum(1 for e in events if e["kind"] == "rag.search" and e["source"] != "prefetch")},
    }


def print_summary(s: Dict[str, Any]):
    print(f"\n{'═' * 100}\n汇总\n{'─' * 100}")
    print(f"  模型请求 {s['llm_calls']} 次  输入 {s['usage']['prompt']}（命中缓存 {s['usage']['hit']}）  输出 {s['usage']['output']}"
          f"  费用 ${s['cost_usd_off_peak']}（非高峰）/ ${s['cost_usd_peak']}（高峰）")
    print("  按角色：")
    for role, v in s["by_role"].items():
        rate = v["hit"] / v["prompt"] if v["prompt"] else 0
        print(f"    {role:<16} {v['calls']}次  in={v['prompt']:<7} hit率={rate:.0%}  out={v['output']:<6} ${v['cost_usd']}")
    print("  输入 token 构成（估算，按字符占比分摊）：")
    total = sum(s["input_tokens_by_part_estimated"].values()) or 1
    for part, tokens in s["input_tokens_by_part_estimated"].items():
        print(f"    {part:<36} {tokens:>7}  {tokens / total:6.1%}  {'█' * round(40 * tokens / total)}")
    print(f"  Jev 意图识别：{s['jev']}")
    print(f"  工具调用：{s['tools']}   RAG：{s['rag']}")


async def run(turns: List[str], output: str):
    load_dotenv(ROOT / ".env")
    from api import state as app_state
    from evaluation.application import application, auth
    from evaluation.reporting import write_report
    from evaluation.token_audit import TokenAudit
    from scripts.evaluate_long_dialogue import drain_maintenance, knowledge_docs

    tracer = Tracer()
    audit = TokenAudit().__enter__()
    restores = instrument(tracer, audit)
    rows = []
    try:
        async with application(documents=knowledge_docs(), maintenance=True) as (http, _):
            skills = app_state.skills
            mode = skills.disclosure() if skills else "none"
            print(f"skill 披露方式：{mode}；已加载：" + ", ".join(f"{s.name}({len(s.to_prompt_block())}字)" for s in (skills.skills if skills else [])))
            print(f"Agent 工具：{sorted(app_state.orchestrator.agent.get_tools())}")
            conv = uuid.uuid4().hex
            headers = auth("alice", subject=f"trace:{conv}", employee_id="1001", department="engineering")
            app_state.workflow.reset_directory(["1001"])
            for index, message in enumerate(turns, 1):
                tracer.turn = index
                before = len(tracer.events)
                started = time.monotonic()
                resp = await http.post("/chat", json={"message": message, "conv_id": conv, "client_request_id": f"{conv}-{index}"}, headers=headers)
                latency = time.monotonic() - started
                await drain_maintenance(app_state)   # 后台记忆维护算在本轮
                body = resp.json() if resp.status_code == 200 else {"response": resp.text, "status": f"http_{resp.status_code}"}
                events = tracer.events[before:]
                print_turn(index, message, body, events, latency)
                rows.append({"turn": index, "user": message, "latency_s": round(latency, 2),
                             "response": {k: body.get(k) for k in ("response", "status", "intent", "intent_group", "tools_used",
                                                                  "knowledge_used", "approval_required", "token_usage", "stage_ms")},
                             "summary": summarize_run(events), "events": events})
    finally:
        for restore in reversed(restores):
            restore()
        audit.__exit__()
    summary = summarize_run(tracer.events)
    print_summary(summary)
    path = write_report(output, {"suite": "workflow-trace", "skill_disclosure": mode, "turns": rows, "summary": summary,
                                 "note": "input_tokens_by_part 为按字符占比的估算；每次请求的 usage 为模型服务返回的真实值。"}, echo=False)
    print(f"\n完整报告（含每次模型请求的 messages）：{path}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-m", "--message", action="append", help="一轮用户消息，可重复")
    p.add_argument("--output", default=f"workflow_trace_{time.strftime('%Y%m%d')}.json")
    args = p.parse_args()
    asyncio.run(run(args.message or DEFAULT_TURNS, args.output))


if __name__ == "__main__":
    main()
