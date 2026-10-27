"""Approval domain objects shared by API and the runtime."""
UNSAFE_TOOLS = {"reset_password", "unlock_account", "request_access", "create_ticket"}

def approval_summary(tool_names, message):
    actions = [name for name in tool_names if name in UNSAFE_TOOLS]
    if not actions:
        return None
    risk = "high" if any(name in {"reset_password", "unlock_account", "request_access"} for name in actions) else "medium"
    return {"actions": actions, "risk": risk, "summary": f"请求执行 {', '.join(actions)}；当前仅生成 dry-run，需人工审批后执行。", "source_message": str(message)[:500]}
