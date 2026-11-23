"""Offline adjacent-request prefix simulation with itemized summary overhead."""

from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from statistics import mean

from fakeredis.aioredis import FakeRedis
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from tokenizers import Tokenizer

from servicedesk_agent.agent.masking.local_pii import model_dir
from servicedesk_agent.agent.memory.context import MemoryContext, Message, MsgRole
from servicedesk_agent.agent.memory.conversation_memory import MemoryManager
from servicedesk_agent.agent.model_router import ModelRouter
from .p1 import experiment, NAMES

LABELS = (
    "full_history_unbounded",
    "sliding_window_baseline",
    "block_truncation_prod",
    "block_plus_summary_prod",
)


def prefix(previous, current):
    for i, (a, b) in enumerate(zip(previous, current)):
        if a != b:
            return i
    return min(len(previous), len(current))


async def dialog(label, case, encode, folder):
    summary_input = summary_output = 0

    async def extract(messages, info):
        nonlocal summary_input, summary_output
        prompt = "\n".join(part.content for message in messages for part in message.parts)
        summary_input += len(encode(prompt))
        # Extract the start of the supplied material without access to gold anchors.
        answer = prompt.split("：\n", 1)[-1][:180]
        summary_output += len(encode(answer))
        return ModelResponse(parts=[TextPart(answer)])

    router = ModelRouter(models=dict.fromkeys(["deepseek-flash", "gpt-6.2-sol"], FunctionModel(extract)))
    manager = MemoryManager(database_url=f"sqlite:///{folder}/context.db", router=router)
    await manager._redis.aclose()
    manager._redis = FakeRedis(decode_responses=True)
    manager.context_mode = "working"
    previous = []
    history = []
    total = cached = peak = violations = state_errors = 0
    retains = []
    try:
        for i, turn in enumerate(case["turns"]):
            if label == LABELS[3]:
                ctx = await manager.get_context("test", "dialog")
            else:
                ctx = MemoryContext(history[-20:] if label == LABELS[1] else history, [], "")
            if label == LABELS[3]:
                turns = ctx.history_turns(limit=None)
            else:
                limit = max(1, len(history)) if label == LABELS[0] else 20
                turns = ctx.history_turns(limit=limit, assistant_chars=600 if label == LABELS[2] else None)
            if label in LABELS[2:]:
                if label == LABELS[2]:
                    violations += max(0, len(turns) - 20)
                state_errors += sum(
                    phrase in content
                    for content in (ctx.summary, *(t["content"] for t in turns))
                    for phrase in ("已生成待审批", "已提交待审批", "已提交审批")
                )
            parts = [
                dict(role="system", content="你是 IT 服务台助手。" + ctx.to_prompt_text(False)),
                *([dict(role="assistant", content=ctx.model_history()[0].parts[0].content)] if ctx.summary else []),
                *turns,
                dict(role="user", content=turn["user"]),
            ]
            tokens = [
                tok
                for p in parts
                for tok in (
                    -(1 + ["system", "user", "assistant"].index(p["role"])),
                    *encode(p["content"]),
                    -4,
                )
            ]
            if label == LABELS[3]:
                violations += int(len(tokens) > router.compaction_budget("deepseek-flash")[0])
            total += len(tokens)
            cached += prefix(previous, tokens)
            peak = max(peak, len(tokens))
            previous = tokens
            if i:
                visible = "\n".join(p["content"] for p in parts)
                retains.append(mean(a in visible for a in case["anchors"]))
            metadata = turn.get("metadata", {})
            history.extend(
                [
                    Message(MsgRole.USER, turn["user"]),
                    Message(MsgRole.ASSISTANT, turn["assistant"], metadata=metadata),
                ]
            )
            if label == LABELS[3]:
                await manager.commit_turn(
                    "test",
                    "dialog",
                    str(i),
                    turn["user"],
                    dict(response=turn["assistant"], **metadata),
                )
        usage = dict(
            input_tokens=total + summary_input,
            cache_read_tokens=cached,
            output_tokens=summary_output,
        )
        return dict(
            input_tokens=usage["input_tokens"],
            cost_usd=router.cost("deepseek-flash", usage),
            prefix_hit_rate=cached / total,
            peak_turn_tokens=peak,
            anchor_retention=mean(retains),
            summary_input_tokens=summary_input,
        ), dict(window_overflow=violations, draft_mislabeled=state_errors)
    finally:
        await manager.close()
        manager.engine.dispose()


async def context(rows, repeats):
    tokenizer = Tokenizer.from_file(str(model_dir() / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()

    @lru_cache(maxsize=8192)
    def encode(text):
        return tokenizer.encode(text, add_special_tokens=False).ids

    async def run(label):
        with TemporaryDirectory() as folder:
            results = [await dialog(label, case, encode, Path(folder)) for case in rows]
        metrics = {k: mean(m[k] for m, _ in results) for k in results[0][0]}
        metrics["peak_turn_tokens"] = max(m["peak_turn_tokens"] for m, _ in results)
        return metrics, {
            k: sum(g[k] for _, g in results) for k in results[0][1]
        } if label in LABELS[2:] else {}

    return await experiment(
        NAMES[2],
        LABELS,
        run,
        repeats,
        "Shorter history can discard early incident identifiers.",
        tokenizer="offline AstrLink token IDs plus role delimiters; provider token counts estimated",
        summary="production compression with deterministic extractive local model; charged uncached",
    )
