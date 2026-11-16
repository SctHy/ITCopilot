"""Workflow failure reporting and cleanup must never manufacture a green check or lose evidence."""
from argparse import Namespace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import dev


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("output/\n.env\n")
    (tmp_path / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    return tmp_path


def test_snapshot_detects_dirty_content_even_when_status_stays_modified(repo):
    (repo / "source.py").write_text("value = 2\n")
    before = dev.source_snapshot(repo)
    (repo / "source.py").write_text("value = 3\n")
    after = dev.source_snapshot(repo)
    assert before["dirty"] and after["dirty"]
    assert before["source_sha256"] != after["source_sha256"]
    (repo / ".env").write_text("SECRET=not-in-source-fingerprint")
    assert dev.source_snapshot(repo) == after
    (repo / "new.py").write_text("untracked = True\n")
    assert dev.source_snapshot(repo)["source_sha256"] != after["source_sha256"]


def test_offline_environment_cannot_accidentally_enable_live_profiles():
    original = {key: "configured" for key in dev.TEST_ENV}
    env = dev.check_environment("backend", original)
    assert not any(key in env for key in dev.TEST_ENV)
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert len(original) == len(dev.TEST_ENV)
    with pytest.raises(ValueError, match="Missing test environment"):
        dev.check_environment("integration", {})
    with pytest.raises(ValueError, match="PostgreSQL"):
        dev.check_environment("postgres", {dev.TEST_ENV[0]: "sqlite:///wrong.db"})


def args(profile="docs", **overrides):
    return Namespace(**({"profile": profile, "tests": [], "dry_run": False, "timeout": 5} | overrides))


def manifest(repo):
    return json.loads(next((repo / dev.OUTPUT).glob("*/manifest.json")).read_text())


def test_failed_command_is_recorded_and_later_commands_do_not_run(repo, monkeypatch):
    marker = repo / "must-not-run"
    monkeypatch.setattr(dev, "commands", lambda *_: [
        ("failure", [sys.executable, "-c", "raise SystemExit(7)"], "."),
        ("later", [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"], ".")])
    assert dev.run_checks(repo, args()) == 1
    result = manifest(repo)
    assert result["status"] == "failed"
    assert result["steps"][0]["exit_code"] == 7
    assert not marker.exists()


def test_source_mutation_during_successful_check_invalidates_result(repo, monkeypatch):
    monkeypatch.setattr(dev, "commands", lambda *_: [
        ("mutation", [sys.executable, "-c", "open('source.py', 'w').write('changed')"], ".")])
    assert dev.run_checks(repo, args()) == 1
    assert manifest(repo)["status"] == "source_changed"


def test_missing_integration_environment_is_failure_with_durable_record(repo, monkeypatch):
    for key in dev.TEST_ENV:
        monkeypatch.delenv(key, raising=False)
    assert dev.run_checks(repo, args("integration")) == 1
    result = manifest(repo)
    assert result["status"] == "error" and not result["steps"]
    assert "Missing test environment" in result["error"]


def test_all_skipped_pytest_is_not_a_green_integration_check(repo, monkeypatch):
    for key in dev.TEST_ENV[1:]:
        monkeypatch.setenv(key, "test-only")

    def skipped(name, argv, *_):
        path = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--junitxml=")))
        path.write_text('<testsuites><testsuite tests="3" skipped="3" failures="0" errors="0"/></testsuites>')
        return {"name": name, "status": "passed", "exit_code": 0}

    monkeypatch.setattr(dev, "run_step", skipped)
    assert dev.run_checks(repo, args("integration")) == 1
    step = manifest(repo)["steps"][0]
    assert step["pytest"]["skipped"] == 3
    assert step["status"] == "failed"


def test_timeout_is_failure_and_kills_descendants(tmp_path):
    marker = tmp_path / "orphan.txt"
    child = f"import time; time.sleep(2); open({str(marker)!r}, 'w').close()"
    script = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
    result = dev.run_step("timeout", [sys.executable, "-c", script], tmp_path,
                          os.environ.copy(), tmp_path / "timeout.log", .2)
    assert result["status"] == "timed_out"
    assert result["exit_code"] == 124
    # Waiting in a subprocess keeps this focused on descendant cleanup, not implementation details.
    subprocess.run([sys.executable, "-c", "import time; time.sleep(2.2)"], check=True)
    assert not marker.exists()


def test_links_ignore_code_fences_external_urls_and_anchors(tmp_path):
    (tmp_path / "good.md").write_text("ok")
    (tmp_path / "index.md").write_text(
        '[ok](good.md#heading) [web](https://example.invalid) [anchor](#title)\n'
        '```md\n[fake](missing-in-example.md)\n```\n[broken](missing.md)\n')
    assert dev.broken_links(tmp_path, ["index.md"]) == ["index.md: missing.md"]


def old_run(repo, name, **changes):
    directory = repo / dev.OUTPUT / name
    directory.mkdir(parents=True)
    metadata = {"tool": dev.TOOL, "status": "passed",
                "ended_at": (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()} | changes
    (directory / "manifest.json").write_text(json.dumps(metadata))
    (directory / "docs.log").write_text("ok")
    return directory


def test_cleanup_only_removes_expired_owned_unpinned_untracked_runs(repo):
    expired = old_run(repo, "expired")
    pinned = old_run(repo, "pinned")
    (pinned / ".keep").touch()
    active = old_run(repo, "active", status="running")
    recent = old_run(repo, "recent", ended_at=dev.now())
    unknown = old_run(repo, "unknown", tool="another-tool")
    malformed = old_run(repo, "malformed")
    (malformed / "manifest.json").write_text("[]")
    extra = old_run(repo, "extra")
    (extra / "valuable.txt").write_text("keep")
    tracked = old_run(repo, "tracked")
    subprocess.run(["git", "-C", str(repo), "add", "-f", str(tracked / "docs.log")], check=True)
    linked = repo / dev.OUTPUT / "linked"
    linked.symlink_to(expired, target_is_directory=True)
    inside_link = old_run(repo, "inside-link")
    (inside_link / "outside.log").symlink_to(repo / "source.py")
    assert dev.clean(repo, 30) == [str(expired.relative_to(repo))]
    assert expired.exists()
    assert dev.clean(repo, 30, apply=True) == [str(expired.relative_to(repo))]
    assert not expired.exists()
    assert all(p.exists() for p in (pinned, active, recent, unknown, malformed, extra, tracked, inside_link))
    assert (repo / "source.py").read_text() == "value = 1\n"


def test_cleanup_rejects_symlinked_output_root(repo, tmp_path_factory):
    (repo / "output").symlink_to(tmp_path_factory.mktemp("outside"), target_is_directory=True)
    with pytest.raises(ValueError, match="symlinked"):
        dev.clean(repo, 30, apply=True)


def test_evaluation_records_outputs_without_claiming_business_success(repo):
    script = repo / "scripts" / "evaluate_example.py"
    script.parent.mkdir(exist_ok=True)
    script.write_text('''import argparse, json
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--output')
out=Path(p.parse_args().output)
out.mkdir(parents=True)
(out/'failed.json').write_text(json.dumps({'passed': False}))
''')
    options = Namespace(script="scripts/evaluate_example.py", arguments=[], file=False, timeout=5)
    assert dev.run_evaluation(repo, options) == 0
    run = next((repo / dev.OUTPUT).iterdir())
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["quality_verdict"].startswith("Not assessed")
    assert json.loads((run / "artifacts/failed.json").read_text()) == {"passed": False}
    manifest["ended_at"] = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    (run / "manifest.json").write_text(json.dumps(manifest))
    assert dev.clean(repo, 30) == [str(run.relative_to(repo))]
    assert run.exists()
    assert dev.clean(repo, 30, apply=True) == [str(run.relative_to(repo))]
    assert not run.exists()


@pytest.mark.parametrize("mutation", ["extra", "changed", "symlink", "pinned", "active", "tracked"])
def test_evaluation_cleanup_preserves_unowned_changed_or_protected_outputs(repo, mutation):
    run = old_run(repo, "evaluation", kind="evaluation", status="completed")
    artifacts = run / "artifacts"
    artifacts.mkdir()
    report = artifacts / "result.json"
    report.write_text('{"passed": false}')
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["owned_files"] = dev.evaluation_files(run)
    if mutation == "extra":
        (artifacts / "manual.json").write_text("keep")
    elif mutation == "changed":
        report.write_text("manual correction")
    elif mutation == "symlink":
        report.unlink()
        report.symlink_to(repo / "source.py")
    elif mutation == "pinned":
        (artifacts / ".keep").touch()
    elif mutation == "active":
        manifest["status"] = "running"
    else:
        subprocess.run(["git", "-C", str(repo), "add", "-f", str(report)], check=True)
    (run / "manifest.json").write_text(json.dumps(manifest))
    assert dev.clean(repo, 30, apply=True) == []
    assert report.exists()


def test_evaluation_cannot_override_managed_output_or_run_outside_scripts(repo):
    options = Namespace(script="source.py", arguments=[], file=False, timeout=5)
    with pytest.raises(ValueError, match="inside scripts"):
        dev.run_evaluation(repo, options)
    (repo / "scripts").mkdir()
    (repo / "scripts/evaluate_example.py").write_text("pass\n")
    options.script = "scripts/evaluate_example.py"
    options.arguments = ["--out=elsewhere"]
    with pytest.raises(ValueError, match="owns --output"):
        dev.run_evaluation(repo, options)


def test_test_selection_cannot_inject_options_or_escape_tests(repo):
    with pytest.raises(ValueError, match="accepts"):
        dev.run_checks(repo, args("backend", tests=["--override-ini=addopts="]))
    with pytest.raises(ValueError, match="inside tests"):
        dev.run_checks(repo, args("backend", tests=["tests/../source.py"]))
    with pytest.raises(ValueError, match="only valid"):
        dev.run_checks(repo, args("frontend", tests=["tests/test_x.py"]))
