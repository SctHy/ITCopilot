import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("args", "metric", "expected"),
    [
        (["concurrency", "--workers", "2", "--resources", "2", "--actions", "7"], "actions", 7),
        (["fault"], "convergence_pct", 100),
        (["sse", "--in-memory", "--clients", "2", "--events-per-stream", "5"], "frames", 10),
    ],
)
def test_acceptance_command(args, metric, expected):
    run = subprocess.run(
        [sys.executable, "-m", "it_copilot.cmd.bench", *args, "--json"],
        capture_output=True,
        text=True,
        timeout=75,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)[metric] == expected
