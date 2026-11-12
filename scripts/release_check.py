"""Run concrete version-specific gates; failed or missing evaluations never permit a canary."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.registry import AgentRegistry
from agents.tools import agent_tools, build_shared_rag_tools
from core.agent_rollouts import AgentRollouts
from core.db import shared_engine
from core.model_router import ModelRouter
from evaluation.attribution import wilson
from evaluation.reporting import REPORTS_DIR, write_report
from tooling.tool_manager import MCPToolManager


def noninferior(candidate, stable):
    c, s = candidate["summary"], stable["summary"]
    if c["trials"] == 0 or s["trials"] == 0:
        return False
    ci, si = wilson(c["passed"], c["trials"]), wilson(s["passed"], s["trials"])
    return c["success_rate"] >= s["success_rate"] - .02 or ci[1] >= si[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16379/0")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    router = ModelRouter()
    manager = MCPToolManager("", router=router)
    registry = AgentRegistry(tools=set(agent_tools()) | set(build_shared_rag_tools(manager)), routes=set(router.config.routes))
    spec = registry.get(args.spec, args.version)
    from workflow.worker import database_url
    rollout = AgentRollouts(shared_engine(database_url(os.getenv("IT_COPILOT_DATA_DIR", str(ROOT / "data")))), registry)
    stable = rollout.get(args.spec)["stable_version"]
    prefix = f"release/{args.spec}@{args.version}"
    reports = {}
    checks = {}
    commands = []
    def run(name, argv):
        log = REPORTS_DIR / f"{prefix}_{name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as stream:
            result = subprocess.run([sys.executable, *argv], cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        commands.append({"gate": name, "argv": argv, "returncode": result.returncode, "log": str(log.relative_to(ROOT))})
        checks[name] = result.returncode == 0
        return result.returncode == 0
    target = ["--agent-spec", args.spec, "--agent-version", args.version]
    run("security", ["scripts/evaluate_security.py", *target])
    if checks["security"]:
        security = json.loads((REPORTS_DIR / "security_v3.json").read_text())
        checks["security"] = security["status"] == "passed" and security["unauthorized_writes"] == security["unauthorized_reads"] == 0
    run("online_ci", ["-m", "pytest", "-q", "tests/test_online_regressions.py", *target])
    for label, version, dataset in (("stable", stable, "application_strata_v3"),
                                     ("candidate", args.version, "application_strata_v3"),
                                     ("online", args.version, "online_regression")):
        filename = f"{prefix}_{label}.json"
        passed = run(label, ["scripts/evaluate_application.py", "--dataset", dataset, "--limit", "1000000",
                            "--repeats", "3", "--redis-url", args.redis_url, "--output", filename,
                            "--agent-spec", args.spec, "--agent-version", version])
        if passed:
            reports[label] = json.loads((REPORTS_DIR / filename).read_text())
            checks[label] = reports[label]["summary"]["trials"] > 0
    checks["noninferior"] = all(label in reports for label in ("stable", "candidate")) and noninferior(reports["candidate"], reports["stable"])
    checks["online_passed"] = "online" in reports and reports["online"]["summary"]["passed"] == reports["online"]["summary"]["trials"]
    report = {"spec": args.spec, "version": args.version, "fingerprint": spec.fingerprint,
              "stable_version": stable, "created_at": datetime.now(timezone.utc).isoformat(),
              "checks": checks, "commands": commands, "status": "passed" if all(checks.values()) else "failed"}
    path = write_report(prefix + ".json", report, echo=False)
    print(f"{report['status']}: {path}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
