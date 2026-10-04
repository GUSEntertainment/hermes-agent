#!/usr/bin/env python3
"""Package explicitly selected board-control changes without editing either tree."""
import argparse
import difflib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_source(root, name):
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"Expected a relative source path: {name}")
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Source escapes its tree: {name}")
    if relative.name.startswith(".env") or relative.name in {"auth.json", "credentials.json"}:
        raise ValueError(f"Credential files cannot be packaged: {name}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Not a regular source file: {name}")
    data = path.read_bytes()
    data.decode("utf-8")
    return data


def package_lane(base, candidate, paths):
    records, patch = [], []
    for name in sorted(set(paths)):
        before, after = read_source(base, name), read_source(candidate, name)
        if before is None and after is None:
            raise ValueError(f"File missing from both trees: {name}")
        if before == after:
            raise ValueError(f"Selected file has no change: {name}")
        # Source deltas are line-oriented and must remain valid git patches.
        for data in (before, after):
            if data and not data.endswith(b"\n"):
                raise ValueError(f"Source must end with a newline: {name}")
        records.append({
            "path": name,
            "before_sha256": digest(before) if before is not None else None,
            "after_sha256": digest(after) if after is not None else None,
        })
        patch.extend(difflib.unified_diff(
            (before or b"").decode().splitlines(keepends=True),
            (after or b"").decode().splitlines(keepends=True),
            fromfile=f"a/{name}" if before is not None else "/dev/null",
            tofile=f"b/{name}" if after is not None else "/dev/null",
        ))
    patch_bytes = "".join(patch).encode()
    return {
        "baseline": str(base), "candidate": str(candidate), "files": records,
        "patch_sha256": digest(patch_bytes),
    }, patch_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for lane in ("gateway", "controller"):
        parser.add_argument(f"--{lane}-base", required=True, type=Path)
        parser.add_argument(f"--{lane}-candidate", required=True, type=Path)
        parser.add_argument(f"--{lane}-file", required=True, action="append")
    parser.add_argument("--output", required=True, type=Path,
                        help="New output directory; existing directories are refused")
    args = parser.parse_args()
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(), "lanes": {}}
    payloads = {}
    for lane in ("gateway", "controller"):
        base = getattr(args, f"{lane}_base").resolve(strict=True)
        candidate = getattr(args, f"{lane}_candidate").resolve(strict=True)
        if not base.is_dir() or not candidate.is_dir() or base == candidate:
            raise ValueError(f"Distinct source directories required for {lane}")
        record, patch = package_lane(base, candidate, getattr(args, f"{lane}_file"))
        record["patch"] = f"{lane}.patch"
        manifest["lanes"][lane] = record
        payloads[record["patch"]] = patch
    # Complete all validation before creating any output.
    args.output.mkdir(parents=True, exist_ok=False)
    for name, data in payloads.items():
        (args.output / name).write_bytes(data)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"packaged": str(args.output.resolve()), "files": {
        lane: len(record["files"]) for lane, record in manifest["lanes"].items()
    }}))


if __name__ == "__main__":
    main()
