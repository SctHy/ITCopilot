"""Three decision policies sharing production tools, write guards and the same metered model."""

import json
import re
import time

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.usage import UsageLimits

from servicedesk_agent.agent.base_agent import BaseAgent
from servicedesk_agent.agent.conversation_state import merge_state, with_login_employee
from servicedesk_agent.agent.intent.recognizer import IntentRecognizer
from servicedesk_agent.agent.masking.model_masking import masking_scope
from servicedesk_agent.agent.model_router import ModelRouter
from servicedesk_agent.agent.prompting import history_messages
from servicedesk_agent.agent.tools import agent_tools
from servicedesk_agent.agent.types import AgentRunDeps, Request

EXTRACT = """仅做意图与参数抽取。输出 JSON 对象，intent 为 apply/diagnose/refuse，
role、employee_id、reason、duration_hours 取自用户对话，缺失填 null。
代办、跳过审批、不支持角色意图为 refuse。已登录员工工号作为本人默认工号。"""


def offline_model(messages, info):
    """Small deterministic transport fixture. It has no access to case IDs or labels."""
    latest = messages[-1].parts[-1]
    prompt = str(getattr(latest, "content", ""))
    if EXTRACT in str(info.instructions):
        role = "pro" if " pro" in prompt else "standard"
        parts = [
            TextPart(
                json.dumps(
                    dict(
                        intent="apply",
                        role=role,
                        employee_id="1001",
                        reason="构建测试",
                        duration_hours=24,
                    )
                )
            )
        ]
    elif getattr(latest, "part_kind", "") == "tool-return":
        parts = [TextPart("已查询本人 DevSuite 许可证。")]
    elif "check_software_access" in {tool.name for tool in info.function_tools}:
        parts = [ToolCallPart("check_software_access", {})]
    elif info.output_tools:
        parts = [ToolCallPart(info.output_tools[0].name, {})]
    else:
        parts = [TextPart("请补充申请用途。")]
    return ModelResponse(parts=parts)


class Meter(WrapperModel):
    def __init__(self, model, row):
        super().__init__(model)
        self.row = row

    async def request(self, messages, model_settings, model_request_parameters):
        start = time.monotonic()
        call = {}
        self.row["api_calls"].append(call)
        try:
            reply = await super().request(messages, model_settings, model_request_parameters)
            call.update(
                {
                    key: getattr(reply.usage, key)
                    for key in ("input_tokens", "output_tokens", "cache_read_tokens")
                }
            )
            call["provider_model"] = reply.model_name
            return reply
        except BaseException as exc:
            call["error"] = type(exc).__name__
            raise
        finally:
            call["ms"] = round((time.monotonic() - start) * 1000, 2)


class TracedAgent(BaseAgent):
    async def _invoke_tool(self, deps, spec, args, tool_id):
        if len(self.log) >= self.limit:
            raise RuntimeError("tool_budget_exhausted")
        entry = dict(tool_name=spec.name, input=dict(args), success=False, turn=self.turn)
        self.log.append(entry)
        result = await super()._invoke_tool(deps, spec, args, tool_id)
        entry.update(deps.traces[-1])
        return result


class Session:
    def __init__(self, arm, store, actor, row, budget, model, model_name, skills):
        self.arm, self.actor, self.row = arm, actor, row
        self.history, self.state = [], {}
        self.model = Meter(model, row)
        self.router = ModelRouter(
            models=dict.fromkeys(("deepseek-flash", "gpt-6.2-sol"), self.model)
        )
        self.router._forced.update(
            dict.fromkeys(("intent_fallback", "agent_write", "agent_read"), model_name)
        )
        self.agent = TracedAgent(
            None, model_name, skills if arm == "C" else None, router=self.router
        )
        self.tools = agent_tools(lambda: store)
        self.agent.set_shared_tools(self.tools)
        self.agent.log, self.agent.limit = row["tools"], budget["max_agent_tool_calls"]
        self.recognizer = IntentRecognizer("", router=self.router)
        self.settings = {
            "temperature": 0.2,
            "max_tokens": 3000,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        self.plain = Agent(
            instructions=f"你是 IT 服务台助手。当前登录员工 {actor.employee_id}。根据实际情况用中文简短回复。",
            tools=[self.agent._as_tool(s) for s in self.tools.values()],
        )
        schemas = {name: tool.input_schema for name, tool in self.tools.items()}
        self.extract = Agent(
            instructions=EXTRACT
            + "\n按对应工具参数 schema 提取字段。\n"
            + json.dumps(schemas, ensure_ascii=False)
        )

    async def fixed(self, req, deps, notice):
        async def tool(name, args=None):
            return await self.agent._invoke_tool(deps, self.tools[name], args or {}, None)

        if notice:
            if "succeeded" in notice:
                state = await tool("check_software_access")
                return "已生效，可以访问。" if state.get("has_access") else "需要人工核对。"
            if "waiting_resource" in notice:
                return "等待库存席位。"
            if "rejected" in notice:
                return "审批已拒绝。"
            return "等待审批。"
        result = await self.extract.run(
            f"本人工号 {self.actor.employee_id}。{req.message}",
            message_history=history_messages(self.history),
            model=self.model,
            model_settings=self.settings,
        )
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", result.output.strip()))
        if data.get("intent") == "refuse":
            return "无法受理该请求。"
        state = await tool("check_software_access")
        if data.get("intent") == "diagnose" and not state.get("expired"):
            return "请转人工进一步诊断。"
        if state.get("has_access") and state.get("scope") == data.get("role"):
            return "已有有效权限。"
        args = {k: data.get(k) for k in ("employee_id", "role", "reason", "duration_hours")}
        if not all(args.values()):
            return "请补充所需范围、用途和期限。"
        out = await tool("request_access", dict(system="DevSuite", **args))
        return "已生成草稿，等待确认审批。" if out.get("requires_approval") else "无法受理该申请。"

    async def respond(self, message, notice="", approvals=()):
        req = Request(
            message=message,
            principal=self.actor,
            history=list(self.history),
            conv_id=self.row["run_id"],
            conversation_approvals=list(approvals),
            thinking="disabled",
        )
        self.agent.turn = len(self.row["turns"])
        deps = AgentRunDeps(req)
        with masking_scope(self.actor.scope_key, req.conv_id):
            if self.arm == "A":
                answer = await self.fixed(req, deps, notice)
            elif self.arm == "B":
                result = await self.plain.run(
                    message,
                    message_history=history_messages(self.history),
                    model=self.model,
                    model_settings=self.settings,
                    deps=deps,
                    usage_limits=UsageLimits(request_limit=20),
                )
                answer = result.output
            else:
                ir = await self.recognizer.recognize(
                    message, self.history, self.state.get("intent_group")
                )
                req.intent, req.intent_group = ir.intent, ir.intent_group
                entities = with_login_employee(
                    ir.entities, self.state, self.actor.employee_id, ir.intent.value
                )
                self.state = merge_state(
                    self.state,
                    ir.intent_group,
                    entities,
                    message,
                    ir.missing_slots,
                    intent=ir.intent,
                )
                req.conversation_state, req.entities = self.state, self.state["entities"]
                req.allowed_writes = ir.requested_actions
                response = await self.agent.handle(req)
                if response.error_code:
                    self.row["errors"].append(response.error_code)
                answer = response.content
        self.history.extend(
            [dict(role="user", content=message), dict(role="assistant", content=answer)]
        )
        self.row["turns"].append(dict(message=message, answer=answer, notice=notice))
        return answer


def fixture_model():
    return FunctionModel(offline_model)
