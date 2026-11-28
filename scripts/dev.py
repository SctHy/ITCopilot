"""Local/CI checks and conservative artifact maintenance; standard library only."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path("output/dev-workflow")
TOOL = "servicedesk-agent-dev-v1"
ENTRY_DOCS = (
    "AGENTS.md", "CLAUDE.md", "README.md", "docs/benchmarks.md",
    ".github/pull_request_template.md",
)
POSTGRES_TESTS = (
    "current_schema", "devsuite_workflow",
    "workflow", "vector_store", "policy_revalidation",
    "isolation_and_history",
    "tenancy", "tenant_scope", "tenant_isolation", "tenant_quotas", "overlapping_grants",
    "turn_records", "grant_reconciler", "policy_change_atomicity",
    "memory_governance", "model_router",
)
PROFILES = ("docs", "backend", "frontend", "local", "postgres", "integration")
TEST_ENV = (
    "SERVICEDESK_AGENT_TEST_DATABASE_URL", "SERVICEDESK_AGENT_TEST_OIDC_ISSUER",
    "SERVICEDESK_AGENT_TEST_GITEA_URL", "SERVICEDESK_AGENT_TEST_GITEA_PASSWORD",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def tracked_files(root):
    return set(git(root, "ls-files", "-z").decode().split("\0")) - {""}


def source_snapshot(root):
    """Hash effective checkout contents, including untracked non-ignored sources."""
    files = set(git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
                .decode().split("\0")) - {""}
    digest = hashlib.sha256()
    count = 0
    for name in sorted(files):
        if name.startswith(("evaluation/reports/", "output/", "logs/", "data/")) or Path(name).name == ".env":
            continue
        path = root / name
        digest.update(name.encode() + b"\0")
        if path.is_symlink():
            digest.update(b"link:" + os.readlink(path).encode())
        elif path.is_file():
            digest.update(str(path.stat().st_mode & 0o111).encode() + b"\0")
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        else:
            digest.update(b"<missing-or-submodule>")
        digest.update(b"\0")
        count += 1
    return {"head": git(root, "rev-parse", "HEAD").decode().strip(),
            "dirty": bool(git(root, "status", "--porcelain", "--untracked-files=all")),
            "source_sha256": digest.hexdigest(), "source_files": count}


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def broken_links(root, names):
    """Check inline relative file links only, not anchors, external URLs or prose."""
    errors = []
    for name in names:
        path = root / name
        if not path.is_file():
            errors.append(f"{name}: missing document")
            continue
        text = re.sub(r"(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$", "", path.read_text())
        for match in re.finditer(r"\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+\"[^\"]*\")?\s*\)", text):
            target = match.group(1) or match.group(2)
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            destination = (path.parent / unquote(url.path)).resolve()
            if not destination.is_relative_to(root.resolve()) or not destination.exists():
                errors.append(f"{name}: {target}")
    return errors


def check_docs(root):
    errors = broken_links(root, ENTRY_DOCS)
    for error in errors:
        print(error)
    print(f"Developer entry links: {len(errors)} error(s)")
    return bool(errors)


def check_environment(profile, environ):
    required = TEST_ENV[:1] if profile == "postgres" else TEST_ENV[1:] if profile == "integration" else ()
    missing = [key for key in required if not environ.get(key)]
    if missing:
        raise ValueError("Missing test environment: " + ", ".join(missing))
    if profile == "postgres" and not environ[TEST_ENV[0]].startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("postgres profile requires a dedicated PostgreSQL test URL")
    env = dict(environ)
    for key in TEST_ENV:
        if key not in required:
            env.pop(key, None)
    env.update(PYTHONUNBUFFERED="1", PYDANTIC_AI_NO_BANNER="1",
               PYTHON_DOTENV_DISABLED="1", SERVICEDESK_AGENT_MASK_MODEL="off",
               SERVICEDESK_AGENT_SEED_DEMO_DOCS="false")
    return env


def commands(profile, tests=()):
    py = sys.executable
    pytest = [py, "-m", "pytest", "-q", "-p", "no:warnings", "-ra"]
    result = []
    if profile in ("docs", "backend", "local"):
        result.append(("docs", [py, "scripts/dev.py", "check-links"], "."))
    if profile in ("backend", "local"):
        result += [("lint", [py, "-m", "ruff", "check", "src", "tests"], "."),
                   ("backend", pytest + list(tests), ".")]
    if profile in ("frontend", "local"):
        result += [("frontend-" + name.replace(":", "-"), ["npm", "run", name], "frontend")
                   for name in ("typecheck", "test", "test:ui", "build")]
    if profile == "postgres":
        result.append(("postgres", pytest + ["tests/integration/test_postgres_engine.py", *(f"tests/test_{name}.py" for name in POSTGRES_TESTS)], "."))
    if profile == "integration":
        result.append(("integration", pytest + ["tests/test_integration_live.py"], "."))
    return result


def stop_process(process):
    """Cancel npm/pytest descendants too; they run in their own POSIX session."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_step(name, argv, cwd, env, log, timeout):
    started = time.monotonic()
    result = {"name": name, "command": argv, "cwd": str(cwd.relative_to(ROOT))
              if cwd.is_relative_to(ROOT) else str(cwd), "status": "running"}
    print(f"[{name}] {shlex.join(argv)}", flush=True)
    with log.open("w") as stream:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            stream.write(str(exc) + "\n")
            result.update(status="error", exit_code=127)
        else:
            try:
                code = process.wait(timeout=timeout)
                result.update(status="passed" if code == 0 else "failed", exit_code=code)
            except subprocess.TimeoutExpired:
                stop_process(process)
                result.update(status="timed_out", exit_code=124)
            except KeyboardInterrupt:
                stop_process(process)
                result.update(status="interrupted", exit_code=130)
    result["duration_s"] = round(time.monotonic() - started, 3)
    result["log"] = log.name
    print(f"[{name}] {result['status']} ({result['duration_s']}s) → {log}", flush=True)
    if result["status"] != "passed":
        print("\n".join(log.read_text(errors="replace").splitlines()[-35:]), flush=True)
    return result


def test_counts(path):
    tree = ET.parse(path).getroot()
    suites = [tree] if tree.tag == "testsuite" else tree.findall("testsuite")
    counts = {key: sum(int(s.get(key, 0)) for s in suites)
              for key in ("tests", "failures", "errors", "skipped")}
    counts["passed"] = counts["tests"] - sum(counts[k] for k in ("failures", "errors", "skipped"))
    return counts


def output_directory(root):
    path = root / OUTPUT
    if any(p.is_symlink() for p in (root / "output", path)):
        raise ValueError("Refusing a symlinked output directory")
    return path


def run_checks(root, args):
    if args.tests and args.profile not in ("backend", "local"):
        raise ValueError("--tests is only valid with backend or local")
    for test in args.tests:
        if not re.fullmatch(r"tests/[\w/.-]+\.py(?:::[\w]+)*", test):
            raise ValueError("--tests accepts tests/*.py paths or ::test_name node IDs")
        candidate = root / test.split("::")[0]
        if not candidate.is_file() or not candidate.resolve().is_relative_to(root / "tests"):
            raise ValueError(f"Test file does not exist inside tests/: {test}")
    steps = commands(args.profile, args.tests)
    if args.dry_run:
        for name, argv, cwd in steps:
            print(f"{name} (cwd={cwd}): {shlex.join(argv)}")
        return 0
    run = output_directory(root) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    manifest = {"tool": TOOL, "started_at": now(), "profile": args.profile,
                "selected_tests": args.tests, "status": "running", "steps": [],
                "python": sys.version.split()[0], "platform": platform.platform()}
    for package in ("pytest", "ruff"):
        try:
            manifest[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            manifest[package] = None
    manifest_path = run / "manifest.json"
    write_json(manifest_path, manifest)
    try:
        manifest["source_before"] = source_snapshot(root)
        env = check_environment(args.profile, os.environ)
        if args.profile in ("frontend", "local"):
            for tool in ("node", "npm"):
                manifest[tool] = subprocess.check_output(
                    [tool, "--version"], env=env, timeout=15, text=True).strip()
        for name, argv, cwd in steps:
            junit = run / f"{name}.xml" if name in ("backend", "postgres", "integration") else None
            if junit:
                argv = argv + [f"--junitxml={junit}"]
            result = run_step(name, argv, root / cwd, env, run / f"{name}.log", args.timeout)
            if junit and junit.exists():
                try:
                    result["pytest"] = test_counts(junit)
                    print(f"[{name}] pytest: {result['pytest']}", flush=True)
                    if result["status"] == "passed" and result["pytest"]["passed"] == 0:
                        result.update(status="failed", reason="No tests passed (empty or all skipped)")
                except (ET.ParseError, ValueError) as exc:
                    result.update(status="error", reason=f"Invalid test results: {exc}")
            elif junit and result["status"] == "passed":
                result.update(status="error", reason="Missing pytest XML")
            manifest["steps"].append(result)
            write_json(manifest_path, manifest)
            if result["status"] != "passed":
                break
        manifest["status"] = "passed" if len(manifest["steps"]) == len(steps) and all(
            s["status"] == "passed" for s in manifest["steps"]) else "failed"
        manifest["source_after"] = source_snapshot(root)
        manifest["source_changed"] = manifest["source_before"] != manifest["source_after"]
        if manifest["source_changed"]:
            manifest["status"] = "source_changed"
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        manifest.update(status="error", error=str(exc))
    finally:
        manifest["ended_at"] = now()
        write_json(manifest_path, manifest)
    print(f"Result: {manifest['status']} → {manifest_path}", flush=True)
    if manifest.get("error"):
        print(manifest["error"], file=sys.stderr)
    return 0 if manifest["status"] == "passed" else 1


def inventory(root):
    tracked = tracked_files(root)
    docs = sorted(set(ENTRY_DOCS) | {str(p.relative_to(root)) for p in (root / "docs").rglob("*.md")})
    reports = root / "evaluation/reports"
    files = sorted(p for p in reports.rglob("*") if p.is_file() and not p.is_symlink())
    # Current source references only: old ledgers and frozen inputs do not imply active use.
    texts = []
    owned = set(git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").decode().split("\0")) - {""}
    for name in sorted(owned | set(docs)):
        if name.startswith(("evaluation/fixtures/", "evaluation/evidence/", "evaluation/reports/")):
            continue
        path = root / name
        if path.suffix in (".md", ".py", ".json", ".yaml", ".yml", ".toml") and path.is_file() and path.stat().st_size <= 2_000_000:
            texts.append((name, path.read_text(errors="replace")))
    rows = []
    for path in files:
        name = str(path.relative_to(root))
        refs = [source for source, text in texts if source != name and
                (name in text or path.name in text)]
        rows.append({"path": name, "tracked": name in tracked, "bytes": path.stat().st_size,
                     "text_references": refs, "action": "review_only"})
    fixture_path = root / "tests/support/fixtures/manifest.json"
    fixtures = json.loads(fixture_path.read_text()) if fixture_path.is_file() else {}
    inputs = [{"name": name, **info, "present": (fixture_path.parent / info["path"]).is_file()}
              for name, info in fixtures.get("inputs", {}).items()]
    return {"broken_document_links": broken_links(root, docs), "reports": rows,
            "frozen_inputs": inputs,
            "limitations": "Current-text references are advisory. Local inputs are absent in a fresh clone. No files are deleted."}


def evaluation_files(directory):
    """Only a closed, regular-file tree can become an expiring evaluation artifact."""
    paths = list(directory.rglob("*"))
    if any(p.is_symlink() or p.name == ".keep" or not (p.is_file() or p.is_dir()) for p in paths):
        raise ValueError("Evaluation contains pinned, linked or non-regular artifacts; retain it")
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths if p.is_file() and p != directory / "manifest.json"}


def run_evaluation(root, args):
    """Record an explicitly requested evaluator; exit zero is not a quality verdict."""
    script = (root / args.script).resolve()
    if not script.is_file() or script.parent != (root / "scripts").resolve() or script.suffix != ".py":
        raise ValueError("evaluate requires a Python script directly inside scripts/")
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if any(a == "--" or (a.startswith("--") and "--output".startswith(a.split("=")[0]))
           for a in arguments):
        raise ValueError("evaluate owns --output; omit it from evaluator arguments")
    run = output_directory(root) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-eval-" + uuid.uuid4().hex[:8])
    run.mkdir(parents=True)
    output = run / ("result.json" if args.file else "artifacts")
    argv = [sys.executable, str(script), *arguments, "--output", str(output)]
    manifest = {"tool": TOOL, "kind": "evaluation", "status": "running", "started_at": now(),
                "quality_verdict": "Not assessed: inspect evaluator business assertions and failures.",
                "source_before": source_snapshot(root), "steps": []}
    write_json(run / "manifest.json", manifest)
    try:
        result = run_step("evaluation-command", argv, root, dict(os.environ, PYTHONUNBUFFERED="1"), run / "command.log", args.timeout)
        manifest["steps"].append(result)
        manifest["status"] = "completed" if result["status"] == "passed" else result["status"]
        if result["status"] == "passed" and not output.exists():
            manifest.update(status="error", error="Evaluator did not create its declared output")
        manifest["source_after"] = source_snapshot(root)
        if manifest["source_before"] != manifest["source_after"]:
            manifest["status"] = "source_changed"
        manifest["owned_files"] = evaluation_files(run)
    except (OSError, ValueError) as exc:
        manifest.update(status="error", error=str(exc))
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
    finally:
        manifest["ended_at"] = now()
        write_json(run / "manifest.json", manifest)
    print(f"Evaluation command: {manifest['status']} → {run / 'manifest.json'}; inspect assertions before accepting results.")
    return 0 if manifest["status"] == "completed" else 1


def clean(root, days, apply=False):
    base = output_directory(root)
    if not base.exists():
        return []
    tracked = tracked_files(root)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    selected = []
    for directory in sorted(base.iterdir()):
        if not directory.is_dir() or directory.is_symlink() or (directory / ".keep").exists():
            continue
        prefix = str(directory.relative_to(root)) + "/"
        if any(name.startswith(prefix) for name in tracked):
            continue
        try:
            manifest = json.loads((directory / "manifest.json").read_text())
            if not isinstance(manifest, dict):
                continue
            ended = datetime.fromisoformat(manifest["ended_at"])
            if manifest.get("tool") != TOOL or manifest.get("status") not in (
                    "passed", "failed", "error", "source_changed", "interrupted", "completed", "timed_out") or ended >= cutoff:
                continue
            if manifest.get("kind") == "evaluation":
                if evaluation_files(directory) != manifest.get("owned_files"):
                    continue
            else:
                children = list(directory.iterdir())
                if any(p.is_symlink() or not p.is_file() or
                       (p.name != "manifest.json" and p.suffix not in (".log", ".xml")) for p in children):
                    continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        selected.append(str(directory.relative_to(root)))
        if apply:
            shutil.rmtree(directory)
    return selected


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    # CI cancellation must stop spawned npm/pytest trees and finalize the record too.
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="Run the same checks as CI")
    check.add_argument("--profile", choices=PROFILES, default="local")
    check.add_argument("--tests", nargs="+", default=[])
    check.add_argument("--timeout", type=positive, default=900, help="Seconds per command")
    check.add_argument("--dry-run", action="store_true")
    sub.add_parser("snapshot", help="Print code provenance, without file contents or secrets")
    sub.add_parser("inventory", help="Read-only document/report review inventory")
    sub.add_parser("check-links", help="Check developer entry-point file links")
    cleanup = sub.add_parser("clean", help="Preview expired temporary checks; --apply deletes them")
    cleanup.add_argument("--days", type=positive, default=30)
    cleanup.add_argument("--apply", action="store_true")
    evaluate = sub.add_parser("evaluate", help="Record a script accepting --output as an expiring run")
    evaluate.add_argument("--file", action="store_true", help="--output is a JSON file rather than a directory")
    evaluate.add_argument("--timeout", type=positive, default=900)
    evaluate.add_argument("script")
    evaluate.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.command == "check":
            return run_checks(ROOT, args)
        if args.command == "evaluate":
            return run_evaluation(ROOT, args)
        if args.command == "check-links":
            return int(check_docs(ROOT))
        if args.command == "snapshot":
            result = source_snapshot(ROOT)
        elif args.command == "inventory":
            result = inventory(ROOT)
        else:
            result = {"apply": args.apply, "paths": clean(ROOT, args.days, args.apply)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
