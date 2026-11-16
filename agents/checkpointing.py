"""Agent-side serialization and execution adapter; no database or workflow imports."""
from dataclasses import asdict
from agents.types import AgentResponse, OrchestratorResult, Request
from agents.intent.taxonomy import IntentCategory, UrgencyLevel


def encode_request(req: Request) -> dict:
    """Freeze model input only; identity and live callbacks are supplied on recovery."""
    names = ('message', 'user_id', 'conv_id', 'context', 'history', 'entities', 'intent_group',
             'intent_confidence', 'intent_source', 'department', 'request_id', 'conversation_state', 'step_outputs', 'conversation_approvals', 'stage_ms')
    fields = {name: getattr(req, name) for name in names}
    return dict(fields, intent=req.intent.value, urgency=req.urgency.value if req.urgency else None,
                allowed_writes=sorted(req.allowed_writes) if req.allowed_writes is not None else None,
                hidden_tools=sorted(req.hidden_tools))


def decode_request(data: dict, *, principal) -> Request:
    fields = dict(data)
    fields['intent'] = IntentCategory(fields['intent'])
    fields['urgency'] = UrgencyLevel(fields['urgency']) if fields['urgency'] else None
    fields['hidden_tools'] = frozenset(fields['hidden_tools'])
    fields['allowed_writes'] = frozenset(fields['allowed_writes']) if fields['allowed_writes'] is not None else None
    return Request(**fields, principal=principal)


def encode(value, req):
    data = asdict(value)
    if data.get('intent') is not None:
        data['intent'] = data['intent'].value
    data['conversation_state'] = req.conversation_state
    return data


def decode(data, req, cls=AgentResponse):
    data = dict(data)
    req.conversation_state = data.pop('conversation_state')
    if data.get('intent') is not None:
        data['intent'] = IntentCategory(data['intent'])
    return cls(**data)


async def execute_step(execute, req, name):
    checkpoint = req.checkpoints
    if not checkpoint:
        return await execute(req)
    inputs = dict(message=req.message, context=req.context, history=req.history, entities=req.entities,
        step_outputs=req.step_outputs, principal=req.principal.public(),
        allowed_writes=sorted(req.allowed_writes) if req.allowed_writes is not None else None,
        hidden_tools=sorted(req.hidden_tools))
    old = await checkpoint.read(name, inputs)
    if old is not None:
        await req.publish('step_restored', {'step_id': name})
        return decode(old, req)
    result = await execute(req)
    if result.success:
        await checkpoint.save(name, inputs, encode(result, req), result.token_usage)
    else:
        await checkpoint.failed(name, inputs, encode(result, req), result.token_usage)
    return result


async def execute_run(orchestrator, req):
    if not req.checkpoints:
        return await orchestrator.run(req)
    inputs = {'request_id': req.request_id, 'context': req.context, 'message': req.message}
    old = await req.checkpoints.read('result', inputs)
    if old is not None:
        return decode(old, req, OrchestratorResult)
    result = await orchestrator.run(req)
    if result.status == 'success':
        # The agent step checkpoint already accounted for its usage.
        await req.checkpoints.save('result', inputs, encode(result, req))
    return result
