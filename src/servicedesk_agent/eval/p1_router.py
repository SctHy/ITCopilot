"""Exercise the production agent's failover loop with local FunctionModels."""

import asyncio
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
import time

import yaml
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart

from servicedesk_agent.agent.base_agent import BaseAgent, ModelUnavailableError
from servicedesk_agent.agent.model_router import (
    DEFAULT_CONFIG,
    DeadlineModel,
    ModelRouter,
    TurnDeadlineExceeded,
)
from servicedesk_agent.agent.types import Request
from .p1 import experiment, NAMES
from .stats import compare, percentile

LABELS = ("single_primary_no_failover", "failover_no_breaker", "router_breaker_failover_prod")


async def router_round(label, folder, requests):
    config = yaml.safe_load(DEFAULT_CONFIG.read_text())
    primary = config["routes"]["agent_read"]["candidates"][0]
    template = config["models"][primary]
    config["models"].update(
        backup=template,
        foreign={**template, "regions": ["us"]},
        public={**template, "data_classes": ["public"]},
    )
    route = config["routes"]["agent_read"]
    route.update(
        candidates=["foreign", "public", primary] + ([] if label == LABELS[0] else ["backup"]),
        request_timeout_s=0.025,
    )
    path = Path(folder) / "routes.yaml"
    path.write_text(yaml.safe_dump(config))
    calls = Counter()

    def stub(name):
        async def respond(messages, info):
            calls[name] += 1
            if name == primary:
                await asyncio.sleep(0.009 if calls[name] % 2 else 0.1)
                raise ModelHTTPError(503, name)
            return ModelResponse(parts=[TextPart("演练完成")])

        return FunctionModel(respond)

    router = ModelRouter(path, models={n: stub(n) for n in config["models"]})
    if label != LABELS[2]:
        router.breakers[primary].threshold = requests + 1
    agent = BaseAgent(None, "", router=router)
    times = []
    success = 0
    for _ in range(requests):
        start = time.perf_counter()
        try:
            response = await agent._call_llm(Request("检查演练服务", allowed_writes=frozenset()))
            success += response.content == "演练完成"
        except ModelUnavailableError:
            pass
        times.append((time.perf_counter() - start) * 1000)
    pick = router.pick("summary") if label != LABELS[2] else router.pick("agent_read")
    breaker = router.breakers[pick.name]
    before = (breaker.fail_count, breaker.state)
    bounded = DeadlineModel(pick.model, deadline=time.monotonic() - 1, request_timeout_s=0.01)
    try:
        await router.run(pick, bounded.request([], {}, None))
    except TurnDeadlineExceeded:
        pass
    else:
        raise AssertionError("deadline probe unexpectedly completed")
    violations = dict(
        noncompliant_calls=calls["public"] + calls["foreign"],
        deadline_breaker_changes=int(before != (breaker.fail_count, breaker.state)),
    )
    metrics = dict(
        availability=success / requests,
        p50_ms=percentile(times, 50),
        p95_ms=percentile(times, 95),
        failed_primary_calls=calls[primary],
        **violations,
    )
    if label == LABELS[2]:
        violations["unavailable"] = requests - success
    return metrics, violations


async def router(rows, repeats, requests=110):
    async def run(label):
        with TemporaryDirectory() as folder:
            return await router_round(label, folder, requests)

    exp = await experiment(
        NAMES[3],
        LABELS,
        run,
        repeats,
        "Open circuits defer recovery probes until cooldown.",
        requests=requests,
        primary_faults="alternating 503 and provider timeout",
    )
    for key in exp.arms[2].metrics:
        compare(exp, exp.arms[1], exp.arms[2], key, "Circuit opening skips failed primary calls.")
    return exp
