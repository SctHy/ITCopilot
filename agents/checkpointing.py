"""Agent-side serialization and execution adapter; no database or workflow imports."""
from dataclasses import asdict
from agents.types import AgentResponse, AgentType, OrchestratorResult
from agents.intent.taxonomy import IntentCategory


def encode(value):
    data = asdict(value)
    for name in ('agent_type', 'primary_agent', 'intent'):
        if data.get(name) is not None:
            data[name] = data[name].value
    for name in ('agent_types', 'supporting_agents'):
        if name in data:
            data[name] = [v.value for v in data[name]]
    return data


def decode(data, cls=AgentResponse):
    data = dict(data)
    for name in ('agent_type', 'primary_agent'):
        if data.get(name) is not None:
            data[name] = AgentType(data[name])
    if data.get('intent') is not None:
        data['intent'] = IntentCategory(data['intent'])
    for name in ('agent_types', 'supporting_agents'):
        if name in data:
            data[name] = [AgentType(v) for v in data[name]]
    return cls(**data)


async def execute_step(execute, req, agent_type, name):
    checkpoint = req.checkpoints
    if not checkpoint:
        return await execute(req, agent_type)
    inputs = dict(message=req.message, context=req.context, history=req.history, entities=req.entities,
        step_outputs=req.step_outputs, principal=req.principal.public(),
        allowed_writes=sorted(req.allowed_writes) if req.allowed_writes is not None else None,
        hidden_tools=sorted(req.hidden_tools), agent_type=agent_type.value)
    old = await checkpoint.read(name, inputs)
    if old is not None:
        await req.publish('step_restored', {'step_id': name})
        return decode(old)
    result = await execute(req, agent_type)
    if result.success:
        await checkpoint.save(name, inputs, encode(result), result.token_usage)
    else:
        await checkpoint.failed(name, inputs, encode(result), result.token_usage)
    return result


async def execute_run(orchestrator, req):
    if not req.checkpoints:
        return await orchestrator.run(req)
    inputs = {'request_id': req.request_id, 'context': req.context, 'message': req.message}
    old = await req.checkpoints.read('result', inputs)
    if old is not None:
        return decode(old, OrchestratorResult)
    result = await orchestrator.run(req)
    if result.status == 'success':
        # Specialist checkpoints already accounted for their usage.
        await req.checkpoints.save('result', inputs, encode(result))
    return result
