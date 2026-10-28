import asyncio
import pytest

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from evaluation.application import application, auth


def access_model():
    async def reply(messages, info):
        if info.output_tools:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "classify_it_request",
                        {
                            "intent": "permission_request",
                            "confidence": 0.95,
                            "entities": {"employee_id": ["1001"], "system": ["GitLab"]},
                        },
                    )
                ]
            )
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "request_access",
                        {
                            "employee_id": "1001",
                            "system": "GitLab",
                            "role": "readonly",
                            "reason": "代码审阅",
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("已生成待审批请求，未执行权限变更。")])

    return FunctionModel(reply)


def test_http_chat_approval_execution_and_replay():
    async def run():
        async with application(model=access_model()) as (http, api):
            payload = {"message": "申请 GitLab 只读权限，工号1001，理由代码审阅", "conv_id": "access", "client_request_id": "concurrent"}
            # Two requests reach the actual API at once, sharing the same receipt id.
            first, replay = await asyncio.gather(http.post("/chat", json=payload), http.post("/chat", json=payload))
            assert first.status_code == replay.status_code == 200
            assert first.json() == replay.json()
            data = first.json()
            assert data["approval_required"]
            assert (await http.get("/business/employees/1001")).json()["grants"] == []
            path = "/approvals/" + data["approval_id"]
            pending = (await http.get(path)).json()
            assert pending["action_requests"][0]["arguments"]["employee_id"] == "1001"
            assert pending["requester"] == "dev:alice" and pending["policy_version"]
            decision = {"decision": "approved", "payload_hash": pending["payload_hash"]}
            assert (await http.post(path, json=decision)).status_code == 403          # alice is not an approver
            approved = (await http.post(path, json=decision, headers=auth("bob"))).json()
            assert approved["execution"] == "succeeded"
            assert (await http.post(path, json=decision, headers=auth("bob"))).json() == approved
            state = (await http.get("/business/employees/1001")).json()
            assert len(state["grants"]) == 1 and state["grants"][0]["role"] == "readonly"
            assert state["access_ledger"][0]["status"] == "active" and state["access_ledger"][0]["expires_at"]
            events = [e["event"] for e in (await http.get(path + "/audit")).json()["events"]]
            assert events == ["created", "approved", "execution_queued", "apply_started", "apply_succeeded"]
            history = await api._memory._get_working_memory("dev:alice", "access")
            assert len(history) == 2  # duplicate request cannot append a second turn
            assert (await http.post("/chat", json={**payload, "message": "different"})).status_code == 409
            # Other employees cannot read alice's approval, trace or account; the same client id is a separate request for them.
            assert (await http.get(path, headers=auth("dave"))).status_code == 403
            assert (await http.get("/trace/" + data["request_id"], headers=auth("dave"))).status_code == 403
            assert (await http.get("/business/employees/1001", headers=auth("dave"))).status_code == 403
            assert (await http.get("/trace/" + data["request_id"], headers=auth("grace"))).json()["found"]  # auditor

    asyncio.run(run())


def test_identity_comes_from_token_not_request_body():
    async def run():
        async with application(model=access_model()) as (http, api):
            forged = {"message": "申请 GitLab 只读权限，工号1001，理由代码审阅", "conv_id": "c", "client_request_id": "forged",
                      "user_id": "dev:erin", "department": "it"}
            # dave asks for alice's employee id while claiming to be erin from IT: the body is ignored,
            # the request runs as dave, and policy refuses to file a change for someone else.
            data = (await http.post("/chat", json=forged, headers=auth("dave"))).json()
            assert not data["approval_required"]
            trace = (await http.get("/trace/" + data["request_id"], headers=auth("dave"))).json()["trace"]
            assert trace["observability"]["owner"] == "dev:dave" and trace["observability"]["department"] == "engineering"
            assert any("本人" in t["output"].get("error", "") for t in trace["tool_traces"])

    asyncio.run(run())


def test_interruption_between_approval_and_receipt_can_retry():
    async def run():
        async with application(model=access_model()) as (http, api):
            commit = api._memory.commit_turn
            count = 0

            async def interrupted(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 1:
                    raise RuntimeError("injected interruption before final receipt")
                return await commit(*args, **kwargs)

            api._memory.commit_turn = interrupted
            payload = {"message": "申请 GitLab 只读权限，工号1001，理由代码审阅", "conv_id": "c", "client_request_id": "interrupted"}
            with pytest.raises(RuntimeError, match="injected"):
                await http.post("/chat", json=payload)
            rid = api._request_id(api.principal_from_token(auth()["Authorization"].split()[1]), "interrupted")
            assert await api._memory.get_turn_receipt("dev:alice", "c", rid) is None
            response = await http.post("/chat", json=payload)
            assert response.status_code == 200 and response.json()["approval_required"]
            assert [e["event"] for e in api._workflow.audit(rid + ":approval")] == ["created"]
            assert api._workflow.employee("1001")["grants"] == []

    asyncio.run(run())


def test_live_stream_reports_tools_before_completion_and_resumes():
    async def run():
        async with application(model=access_model()) as (http, api):
            payload = {"message": "申请 GitLab 只读权限，工号1001，理由代码审阅", "conv_id": "s", "client_request_id": "stream"}
            events = []
            async with http.stream("POST", "/chat/stream", json=payload) as response:
                async for line in response.aiter_lines():
                    if line.startswith("id: "):
                        events.append([line[4:], None])
                    elif line.startswith("event: "):
                        events[-1][1] = line[7:]
            kinds = [kind for _, kind in events]
            assert kinds[0] == "status" and kinds[-1] == "completed"
            assert kinds.index("tool_started") < kinds.index("tool_finished") < kinds.index("approval_required") < kinds.index("completed")
            # Reconnect after the tool_started event: only later events are replayed and nothing re-executes.
            resume_id = events[kinds.index("tool_started")][0]
            rid = api._request_id(api.principal_from_token(auth()["Authorization"].split()[1]), "stream")
            replay = await http.get(f"/chat/stream/{rid}", headers={**auth(), "Last-Event-ID": resume_id})
            replayed = [line[7:] for line in replay.text.splitlines() if line.startswith("event: ")]
            assert replayed == kinds[kinds.index("tool_started") + 1:]
            assert (await http.get(f"/chat/stream/{rid}", headers=auth("dave"))).status_code == 403
            assert len(api._workflow.list(api.principal_from_token(auth("bob")["Authorization"].split()[1]), "pending")) == 1

    asyncio.run(run())


def test_events_reach_a_real_tcp_client_before_the_turn_completes():
    """ASGITransport buffers whole bodies, so timing is checked through a real uvicorn socket."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "scripts/benchmark_stream.py"
    spec = importlib.util.spec_from_file_location("benchmark_stream", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = asyncio.run(module.measure(runs=1, latency=0.3))[0]
    # Three model calls of 0.3s: the tool event must arrive about one call before completion.
    assert row["first_progress_s"] < row["first_tool_s"] < row["completed_s"] - 0.2
