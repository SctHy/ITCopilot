"""Frozen audits and holdout selection must fail closed if their inputs drift."""
import hashlib
import json

import pytest

from evaluation.frozen import frozen_input


def fixture_manifest(root, path="inputs.json"):
    content = b'{"historical": true}\n'
    (root / "inputs.json").write_bytes(content)
    (root / "manifest.json").write_text(json.dumps({"schema_version": 1, "inputs": {
        "example": {"path": path, "sha256": hashlib.sha256(content).hexdigest()}}}))


def test_missing_or_changed_input_cannot_be_replaced_by_current_output(tmp_path):
    fixture_manifest(tmp_path)
    path = frozen_input("example", directory=tmp_path)
    path.write_text('{"historical": false}')
    with pytest.raises(ValueError, match="checksum mismatch"):
        frozen_input("example", directory=tmp_path)
    path.unlink()
    with pytest.raises(FileNotFoundError, match="Restore"):
        frozen_input("example", directory=tmp_path)


def test_manifest_cannot_resolve_outside_fixture_directory(tmp_path):
    fixture_manifest(tmp_path, "../outside.json")
    with pytest.raises(ValueError, match="escapes"):
        frozen_input("example", directory=tmp_path)


def test_dedup_inputs_are_available_without_old_reports():
    frozen = json.loads(frozen_input("masking-prior").read_text())
    assert len(set(frozen["previous_document_hashes"])) == 15663
    assert len(set(frozen["nemotron_10000_hashes"])) == 10000
    # These are exclusion hashes, not gold labels or predictions that leak into selection.
    assert all(len(h) == 64 and set(h) <= set("0123456789abcdef")
               for key in ("previous_document_hashes", "nemotron_10000_hashes") for h in frozen[key])
