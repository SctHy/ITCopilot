"""Checksum-verified inputs for reproducible selection and historical audits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def frozen_input(name: str, *, directory: Path = FIXTURES) -> Path:
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported frozen-input manifest")
    entry = manifest["inputs"][name]
    path = directory / entry["path"]
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError(f"Frozen input escapes fixture directory: {name}")
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing frozen input {name}: {path}. Restore the checksum-matching fixture; "
            "do not substitute a new run or silently skip deduplication.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise ValueError(f"Frozen input checksum mismatch: {name} ({path})")
    return path
