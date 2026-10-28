"""The security regression suite runs as a test so CI blocks any regression (see scripts/evaluate_security.py)."""
import asyncio
import importlib.util
import pathlib


def test_security_suite_has_no_unauthorized_reads_or_writes():
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts/evaluate_security.py"
    spec = importlib.util.spec_from_file_location("evaluate_security", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = asyncio.run(module.run_cases())
    failed = [(c["id"], c["checks"], c["error"]) for c in cases if not c["passed"]]
    assert not failed
    assert len(cases) >= 20
