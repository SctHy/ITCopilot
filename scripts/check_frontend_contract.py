#!/usr/bin/env python3
"""Compare frontend toolchains without coupling either build to a sibling checkout."""
import argparse
import json
from pathlib import Path

CORE = ("react", "react-dom", "typescript", "vite", "vitest", "tailwindcss", "@tailwindcss/vite", "@vitejs/plugin-react", "@base-ui/react", "@tanstack/react-query", "@tanstack/react-router", "@testing-library/react", "@testing-library/user-event", "jsdom", "lucide-react")

def compare(left, right):
    a, b = (json.loads(path.read_text()) for path in (left, right))
    da = a.get("dependencies", {}) | a.get("devDependencies", {})
    db = b.get("dependencies", {}) | b.get("devDependencies", {})
    errors = [f"{key}: {da.get(key)} != {db.get(key)}" for key in CORE if key not in da or da.get(key) != db.get(key)]
    if a.get("engines") != b.get("engines"):
        errors.append("Node/npm engines differ")
    for key in ("test", "test:ui", "typecheck", "build", "check"):
        if a["scripts"].get(key) != b["scripts"].get(key):
            errors.append(f"script {key} differs")
    return errors

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("peer", type=Path, help="Other project's frontend package.json")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    local = root / ("frontend/package.json" if (root / "frontend").is_dir() else "apps/web/package.json")
    problems = compare(local, args.peer)
    print("\n".join(problems) if problems else "Frontend core dependencies, engines and check commands match")
    raise SystemExit(bool(problems))
