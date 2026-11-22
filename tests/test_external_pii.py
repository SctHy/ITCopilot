"""External-corpus adapter invariants, independent of detection predictions."""
import pytest

from tests.support.external_pii import digest, select_rows, validate_spans


def test_original_unicode_offsets_and_duplicate_annotations():
    s = {"start": 1, "end": 3, "text": "张伟", "label": "person"}
    assert validate_spans("🙂张伟。", [s, s])[0] == [(1, 3, "person")]


@pytest.mark.parametrize("start,end,value", [(0, 8, "Alice"), (-1, 5, "Alice"), (2, 2, ""), (False, 5, "Alice"), (0, 4, "Alice"), (0, 5, None)])
def test_invalid_annotations_reject_document_without_offset_search(start, end, value):
    with pytest.raises(ValueError):
        validate_spans("Alice", [{"start": start, "end": end, "text": value, "label": "person"}])


def test_numeric_and_case_only_validation_does_not_move_offsets():
    source = [{"start": 0, "end": 7, "text": "spanish", "label": "race_ethnicity"},
              {"start": 8, "end": 10, "text": 21, "label": "age"}]
    spans, counts = validate_spans("Spanish 21", source, allow_case_only=True)
    assert spans == [(0, 7, "race_ethnicity"), (8, 10, "age")]
    assert counts == {"case_only_value_difference": 1, "scalar_value_rendered": 1}
    with pytest.raises(ValueError):
        validate_spans("Spanish 21", source)


def test_selection_is_hash_based_and_excludes_prior_duplicates_before_sampling():
    rows = [{"id": i, "text": s} for i, s in enumerate(["old", "new A", "new B", "new A"])]
    selected, duplicates, unique = select_rows(rows, {digest("old")}, limit=1)
    assert duplicates == [0, 3] and unique == 2 and len(selected) == 1
    assert selected == select_rows(rows, {digest("old")}, limit=1)[0]
    assert selected[0]["text"] != "old"


def test_comparison_preserves_distinct_rows_with_reused_source_uid(tmp_path):
    import json
    from tests.support.compare_masking_runs import read
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({"overall": {}}))
    docs = tmp_path / "documents.jsonl"
    docs.write_text('\n'.join(json.dumps({"id": "reused", "source_index": i}) for i in [12, 34]))
    assert set(read(path)[1]) == {12, 34}
    docs.write_text('\n'.join(json.dumps({"id": "reused"}) for _ in range(2)))
    with pytest.raises(AssertionError, match="Duplicate document identities"):
        read(path)


def test_comparison_does_not_report_zero_false_positive_rate_without_negatives(tmp_path):
    import json
    from tests.support.compare_masking_runs import compare
    row = {"id": 1, "hash": "fixed", "gold_spans": [[0, 1, "ID"]], "full_flags": [True],
           "tp": 1, "gold": 1, "predicted": 1, "fully_covered": 1}
    overall = dict.fromkeys(["f1", "full_coverage_rate", "precision", "recall"], 1)
    overall["negative_change_rate"] = None
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({"modes": {"on": {"overall": overall, "documents": [row]}}}))
    result = compare(path, path)
    assert result["delta_percentage_points"]["negative_change_rate"] is None
    assert result["delta_percentage_points"]["f1"] == 0


def test_frozen_source_overlay_loads_the_hashed_implementation(tmp_path):
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    import os
    package = Path("src/it_copilot/agent/masking")
    shutil.copytree(root / "src/it_copilot", tmp_path / "src/it_copilot")
    with (tmp_path / package / "pii_rules.py").open("a") as stream:
        stream.write('\nOVERLAY_TEST_MARKER = "frozen"\n')
    subprocess.run([sys.executable, "-c", """
import sys
sys.argv = ['evaluation', '--source-root', sys.argv[1]]
import tests.support.evaluate_masking_external as evaluator
evaluator.source_hashes()
from it_copilot.agent.masking import pii_rules
assert pii_rules.OVERLAY_TEST_MARKER == 'frozen'
""", str(tmp_path)], cwd=root, env={**os.environ, "PYTHONPATH": str(tmp_path / "src")}, check=True, capture_output=True, text=True)
