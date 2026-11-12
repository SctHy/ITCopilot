"""The release CLI reaches the requested version through a real nested pytest run."""
from pathlib import Path
import subprocess
import sys

from agents.registry import default_spec

ROOT = Path(__file__).resolve().parents[1]
CASE = "tests/test_online_regressions.py::test_promoted_export_is_readable_and_executable_in_the_application"


def test_online_regression_cli_selects_target_and_fails_for_missing_version():
    spec = default_spec()
    common = [sys.executable, "-m", "pytest", "-q", CASE, "--agent-spec", spec.name]
    valid = subprocess.run([*common, "--agent-version", spec.version], cwd=ROOT, text=True, capture_output=True)
    assert valid.returncode == 0, valid.stdout + valid.stderr
    missing = subprocess.run([*common, "--agent-version", "999.999.999"], cwd=ROOT, text=True, capture_output=True)
    output = missing.stdout + missing.stderr
    assert missing.returncode == 1 and "KeyError" in output and "999.999.999" in output, output
    incomplete = subprocess.run(common, cwd=ROOT, text=True, capture_output=True)
    assert incomplete.returncode == 4 and "must be supplied together" in incomplete.stderr
