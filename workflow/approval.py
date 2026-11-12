"""Approval domain objects shared by API and the runtime."""
from workflow.actions import WRITE_TOOLS, risk_of

# Kept as a name for existing callers; the catalog in workflow.actions is the source.
UNSAFE_TOOLS = WRITE_TOOLS


def approval_summary(tool_names, message):
    actions = [name for name in tool_names if name in UNSAFE_TOOLS]
    if not actions:
        return None
    return {
        "actions": actions,
        "risk": risk_of(actions),
        "summary": f"请求执行 {', '.join(actions)}；当前仅生成 dry-run，需人工审批后执行。",
        "source_message": str(message)[:500],
    }


def approval_from_traces(traces, message):
    requests = [
        {"tool_name": t["tool_name"], "arguments": t["input"]}
        for t in traces
        if t.get("success") and t.get("tool_name") in UNSAFE_TOOLS
    ]
    summary = approval_summary([r["tool_name"] for r in requests], message)
    return {**summary, "action_requests": requests} if summary else None


MAX_PROVENANCE_TOOLS = 12
MAX_PROVENANCE_CITATIONS = 20


def _citation(hit):
    if not isinstance(hit, dict):
        return None
    return hit.get("citation") or hit.get("title") or hit.get("chunk_id")


def provenance(traces, *, request_id, intent=None, knowledge=(), trace_id=None, channel=None):
    """Why the Agent proposed this approval, frozen into the immutable ``created`` audit entry.

    The full trace lives in Redis with a TTL; this is the part an investigation needs later
    ("was this request steered by a knowledge-base passage?"): what the intent layer allowed,
    which knowledge was in front of the model, and every tool call of the turn with its raw
    arguments (credentials redacted). Not part of the payload hash: approvers decide the
    frozen actions, not the explanation."""
    from core.redaction import redact

    citations = [_citation(hit) for hit in knowledge or ()]
    for trace in traces:
        if trace.get("tool_name") == "search_knowledge_base" and trace.get("success"):
            output = trace.get("output") if isinstance(trace.get("output"), dict) else {}
            citations += [_citation(hit) for hit in output.get("results") or ()]
    seen, unique = set(), []
    for citation in citations:
        if citation and citation not in seen:
            seen.add(citation)
            unique.append(str(citation)[:200])
    tools = [{"tool": t.get("tool_name"), "success": bool(t.get("success")), "outcome": t.get("outcome"),
              "input": redact(t.get("input")), **({"error": str(t["error"])[:200]} if t.get("error") else {})}
             for t in traces[:MAX_PROVENANCE_TOOLS]]
    body = {"request_id": request_id, "knowledge": unique[:MAX_PROVENANCE_CITATIONS], "tools": tools,
            "tool_calls": len(traces)}
    if intent is not None:
        actions = getattr(intent, "requested_actions", None)
        body["intent"] = {"intent": getattr(getattr(intent, "intent", None), "value", None), "group": getattr(intent, "intent_group", None),
                          "source": getattr(intent, "recognition_source", None), "confidence": round(float(getattr(intent, "confidence", 0) or 0), 4),
                          "requested_actions": sorted(actions) if actions is not None else None}
    if trace_id:
        body["trace_id"] = trace_id
    if channel:
        body["channel"] = channel
    return body
