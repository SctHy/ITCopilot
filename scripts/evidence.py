"""Verify or restore selected historical evidence bundles into a new directory."""
from __future__ import annotations

import argparse
import hashlib
import gzip
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
INDEX = Path("docs/archive/2026-11-16-file-index.json.gz")


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_member(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not name.startswith("evaluation/reports/"):
        raise ValueError(f"Unsafe evidence member: {name}")


def verify(root, index, name):
    package = index["packages"][name]
    path = root / package["path"]
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Evidence package escapes repository")
    with path.open("rb") as stream:
        if digest(stream) != package["sha256"]:
            raise ValueError(f"Evidence archive checksum mismatch: {name}")
    expected = {e["member"]: e for e in index["entries"]
                if e.get("package") == name and e["action"] == "package"}
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) != len(expected) or {m.name for m in members} != set(expected):
            raise ValueError(f"Evidence member list differs: {name}")
        for member in members:
            safe_member(member.name)
            if not member.isfile():
                raise ValueError(f"Evidence must contain regular files: {member.name}")
            entry = expected[member.name]
            with archive.extractfile(member) as stream:
                if member.size != entry["bytes"] or digest(stream) != entry["sha256"]:
                    raise ValueError(f"Evidence member checksum mismatch: {member.name}")
    aliases = [e for e in index["entries"] if e.get("package") == name and e["action"] == "deduplicate"]
    for entry in aliases:
        safe_member(entry["path"])
        if entry["member"] not in expected or expected[entry["member"]]["sha256"] != entry["sha256"]:
            raise ValueError(f"Evidence alias lacks matching content: {entry['path']}")
    return path, aliases


def extract(root, index, name, destination):
    path, aliases = verify(root, index, name)
    destination = destination.absolute()
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError("Extraction requires a new directory without symlinked parents")
    destination.mkdir(parents=True)
    # The bundle was checked member by member; data filtering also rejects unsafe tar metadata.
    with tarfile.open(path, "r:gz") as archive:
        archive.extractall(destination, filter="data")
    for entry in aliases:
        target = destination / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(destination / entry["member"], target)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify", "extract"))
    parser.add_argument("package", nargs="?")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    with gzip.open(ROOT / INDEX, "rt", encoding="utf-8") as stream:
        index = json.load(stream)
    try:
        if args.command == "verify":
            for name in [args.package] if args.package else index["packages"]:
                verify(ROOT, index, name)
                print(f"Verified: {name}")
        else:
            if not args.package:
                parser.error("extract requires a package name")
            destination = args.destination or ROOT / "output/evidence" / args.package
            print(extract(ROOT, index, args.package, destination))
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        parser.exit(1, f"Evidence unavailable or invalid: {exc}\n")


if __name__ == "__main__":
    main()
