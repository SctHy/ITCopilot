"""Evaluation-gated skill improvement loop (see evaluation/improvement.py and ADR 0011).

    python scripts/self_improve.py --rounds 3 --repeats 2 --output self_improve_run1.json
    python scripts/self_improve.py --promote evaluation/improvement/runs/<run_id>/round_2/skills

Splits, all on the same application lifespan and graders as the other suites:
- dev: the frozen τ-bench-style tasks (simulated_v4); failures here are shown to the proposer;
- holdout: fresh tasks from the same generator with another seed, minus any task identical to a
  dev task; never shown to the proposer, used only as a gate;
- safety: the N stratum of application_strata_v3 (requests that must not lead to a write).
Skills are switched by reloading the shared SkillManager from a candidate directory; the intent
cache is cleared before each evaluation so evaluations are independent samples. Each round re-runs
the incumbent next to the candidate, so a round costs two dev runs (times --repeats).
The live skills/ directory is not modified unless --promote is given; --promote refuses if skills/
changed since the run started.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

from core.llm import build_model  # noqa: E402
from evaluation.application import application  # noqa: E402
from evaluation.improvement import Gate, Ledger, improve, llm_proposer, promote  # noqa: E402
from evaluation.reporting import DATASETS_DIR, ROOT, write_report  # noqa: E402
from evaluation.simulated import generate_tasks  # noqa: E402

IMPROVEMENT_DIR = ROOT / "evaluation/improvement"


def holdout_tasks(dev, seed, size):
    seen = {(t["opening"], json.dumps(t["facts"], ensure_ascii=False, sort_keys=True)) for t in dev}
    fresh = [t for t in generate_tasks(seed) if (t["opening"], json.dumps(t["facts"], ensure_ascii=False, sort_keys=True)) not in seen]
    return [{**t, "id": "h-" + t["id"]} for t in fresh[:size]]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16379/0")
    parser.add_argument("--planning", default="generalist")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--dev-limit", type=int, default=1000)
    parser.add_argument("--holdout-seed", type=int, default=20261103)
    parser.add_argument("--holdout-size", type=int, default=30)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--min-gain", type=int, default=2)
    parser.add_argument("--max-p", type=float, default=0.1, help="one-sided McNemar p-value the dev gain must reach")
    parser.add_argument("--repeats", type=int, default=1, help="runs per dev/holdout task; 2 is recommended")
    parser.add_argument("--holdout-tolerance", type=int, default=0)
    parser.add_argument("--output", default="self_improve.json")
    parser.add_argument("--promote", default="", help="copy an accepted skills directory into skills/ and exit")
    parser.add_argument("--force", action="store_true", help="with --promote: overwrite skills/ even if it changed since the run")
    args = parser.parse_args()
    skills_dir = pathlib.Path(os.getenv("IT_COPILOT_SKILLS_DIR", ROOT / "skills"))
    if args.promote:
        print("promoted:", promote(pathlib.Path(args.promote), skills_dir, force=args.force))
        return
    load_dotenv(ROOT / ".env")
    from scripts.evaluate_application import trial
    from scripts.evaluate_simulated import run_task

    dev = json.loads((DATASETS_DIR / "simulated_v4.json").read_text(encoding="utf-8"))[: args.dev_limit]
    holdout = holdout_tasks(dev, args.holdout_seed, args.holdout_size)
    safety = [c for c in json.loads((DATASETS_DIR / "application_strata_v3.json").read_text(encoding="utf-8")) if c.get("stratum") == "N"]
    model = build_model(os.getenv("OPENAI_API_KEY", "sk-local"), os.getenv("OPENAI_BASE_URL"), os.getenv("OPENAI_MODEL", "deepseek-chat"))
    proposer_model = build_model(os.getenv("OPENAI_API_KEY", "sk-local"), os.getenv("OPENAI_BASE_URL"), os.getenv("EVAL_IMPROVER_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-chat"))
    gate = Gate(min_gain=args.min_gain, max_p=args.max_p, holdout_tolerance=args.holdout_tolerance, repeats=args.repeats)

    async with application(redis_url=args.redis_url, documents=json.loads((DATASETS_DIR / "rag_v2_corpus.json").read_text())) as (http, state):
        state.orchestrator.planning_mode = args.planning

        async def evaluate(skills, split):
            state.skills.root_dir = skills.resolve()
            state.skills.load()
            state.orchestrator._intent_recognizer._cache.clear()
            rows = []
            for item in {"dev": dev, "holdout": holdout, "safety": safety}[split]:
                state.tool_manager.invalidate_cache("knowledge_search")
                if split == "safety":
                    row = await trial(http, state, item, args.planning, 0)
                else:
                    row = await run_task(http, state, item, args.planning, model, args.max_turns)
                rows.append(row)
                print(f"{skills.parent.name} {split} {item['id']}: {row['passed']}", flush=True)
            return rows

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report = await improve(
            skills_dir=skills_dir, workdir=IMPROVEMENT_DIR / "runs" / run_id, evaluate=evaluate,
            propose=llm_proposer(proposer_model), tasks={t["id"]: t for t in dev},
            ledger=Ledger(IMPROVEMENT_DIR / "ledger.jsonl"), rounds=args.rounds, gate=gate, run_id=run_id,
        )
    report.update(suite="self-improve", planning=args.planning, model=os.getenv("OPENAI_MODEL"),
                  proposer_model=os.getenv("EVAL_IMPROVER_MODEL") or os.getenv("OPENAI_MODEL"),
                  splits={"dev": len(dev), "holdout": len(holdout), "safety": len(safety)}, holdout_seed=args.holdout_seed,
                  limitations="Skill text only; dev and holdout come from one task generator, so holdout tests new samples, not new task types. The incumbent is re-run each round and dev must reach p_one_sided <= max_p; an accepted gain is still overstated by selection.")
    write_report(args.output, report, echo=False)
    print(json.dumps({k: report[k] for k in ("stopped", "final", "final_skills_dir")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
