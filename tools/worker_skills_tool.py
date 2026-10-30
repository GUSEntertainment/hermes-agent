"""Read-only, profile-local skill loading for workspace-confined makers.

This module intentionally does not delegate to :mod:`tools.skills_tool`: the
legacy loader can discover plugins/external directories, preprocess content,
register environment passthrough, capture setup secrets, update review state,
and bump usage counters.  None of those side effects or authorities belong in
a confined maker process.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

from hermes_constants import get_hermes_home
from tools.registry import registry

_ALLOWED_SUPPORT_DIRS = ("references", "templates", "scripts", "assets")
_MAX_SKILL_BYTES = 1024 * 1024
_MAX_LINKED_FILE_BYTES = 4 * 1024 * 1024


class WorkerSkillsError(PermissionError):
    """The maker skill request is outside the read-only authority boundary."""


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _active_profile_home() -> tuple[str, Path]:
    """Return profile identity from authenticated durable worker admission."""
    from agent.private_worker_mode import get_authenticated_worker_contract

    contract = get_authenticated_worker_contract()
    if not contract or contract.get("workspace_confinement") is not True:
        raise WorkerSkillsError("worker skills require authenticated confined admission")
    profile = contract.get("profile")
    if not isinstance(profile, str) or not profile:
        raise WorkerSkillsError("worker skills require an admitted named profile")
    home = Path(str(contract.get("profile_home", "")))
    active_home = get_hermes_home()
    if active_home.resolve() != home.resolve():
        raise WorkerSkillsError(
            "HERMES_HOME is not bound to the admitted profile directory"
        )
    if home.name != profile or home.parent.name != "profiles":
        raise WorkerSkillsError(
            "admitted profile home is not a local named profile directory"
        )
    if home.is_symlink():
        raise WorkerSkillsError("active profile HERMES_HOME must not be a symlink")
    if not home.is_dir():
        raise WorkerSkillsError("active profile HERMES_HOME must be an existing directory")
    return profile, home


def check_worker_skills_requirements() -> bool:
    """Fail-closed registry availability check for the maker-only toolset."""
    try:
        _active_profile_home()
        return True
    except Exception:
        return False


def _validate_relative(value: str, *, label: str, allow_nested: bool = True) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        raise WorkerSkillsError(f"{label} must be a non-empty relative path")
    candidate = value.strip()
    posix = PurePosixPath(candidate)
    windows = PureWindowsPath(candidate)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or "\\" in candidate
        or ":" in candidate
    ):
        raise WorkerSkillsError(f"{label} must be a local relative path")
    parts = posix.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise WorkerSkillsError(f"{label} cannot contain traversal components")
    if not allow_nested and len(parts) != 1:
        raise WorkerSkillsError(f"{label} must be a single path component")
    return parts


def _skills_root(home: Path) -> Path:
    root = home / "skills"
    if root.is_symlink():
        raise WorkerSkillsError("profile skills directory must not be a symlink")
    return root


def _assert_local_nonsymlink(root: Path, target: Path) -> None:
    """Require target and every existing descendant of root to be non-symlinks."""
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise WorkerSkillsError("skill path escapes the local profile skills directory") from exc
    current = root
    if current.is_symlink():
        raise WorkerSkillsError("profile skills directory must not be a symlink")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise WorkerSkillsError(f"symlink paths are not allowed: {relative}")


def _read_regular_text(root: Path, path: Path, *, max_bytes: int) -> str:
    _assert_local_nonsymlink(root, path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise WorkerSkillsError(f"cannot safely read local skill file: {path.name}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise WorkerSkillsError("skill content must be a regular file")
        if info.st_size > max_bytes:
            raise WorkerSkillsError("skill content exceeds the read-only size limit")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read(max_bytes + 1)
    except UnicodeDecodeError as exc:
        raise WorkerSkillsError("worker skills can load UTF-8 text files only") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---"):
        return {}, content
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content
    closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if closing is None:
        return {}, content
    parsed = yaml.safe_load("".join(lines[1:closing])) or {}
    if not isinstance(parsed, dict):
        parsed = {}
    return parsed, "".join(lines[closing + 1 :])


def _metadata(skill_md: Path, root: Path) -> dict[str, Any]:
    content = _read_regular_text(root, skill_md, max_bytes=_MAX_SKILL_BYTES)
    frontmatter, body = _parse_frontmatter(content)
    raw_name = frontmatter.get("name") or skill_md.parent.name
    name = str(raw_name).strip()
    # A metadata name is an identifier, never another lookup path or namespace.
    if not name or len(name) > 64 or not all(c.isalnum() or c in "_-" for c in name):
        raise WorkerSkillsError("skill frontmatter contains an unsafe name")
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        description = next(
            (line.strip() for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")),
            "",
        )
    if len(description) > 1024:
        description = description[:1021] + "..."
    rel_dir = skill_md.parent.relative_to(root)
    category = "/".join(rel_dir.parts[:-1]) or None
    return {
        "name": name,
        "description": description,
        "category": category,
        "skill_md": skill_md,
        "content": content,
        "frontmatter": frontmatter,
    }


def _iter_local_skills(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise WorkerSkillsError("profile skills path is not a directory")
    results: list[dict[str, Any]] = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        # Never descend through a symlink, including one that resolves locally.
        dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
        if "SKILL.md" not in files or base == root:
            continue
        skill_md = base / "SKILL.md"
        if skill_md.is_symlink():
            continue
        try:
            results.append(_metadata(skill_md, root))
        except (OSError, WorkerSkillsError, yaml.YAMLError):
            # Listing cannot safely expose malformed or questionable entries.
            continue
    return results


def _public_metadata(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in ("name", "description", "category")}


def worker_skills_list(category: str | None = None) -> str:
    """List only skills in the active confined maker profile's local tree."""
    try:
        _, home = _active_profile_home()
        if category is not None:
            category = "/".join(_validate_relative(category, label="category"))
        root = _skills_root(home)
        skills = _iter_local_skills(root)
        if category is not None:
            skills = [item for item in skills if item["category"] == category]
        public = sorted(
            (_public_metadata(item) for item in skills),
            key=lambda item: (item.get("category") or "", item["name"]),
        )
        categories = sorted({item["category"] for item in public if item["category"]})
        return json.dumps(
            {
                "success": True,
                "skills": public,
                "categories": categories,
                "count": len(public),
                "hint": "Use worker_skill_view(name) to load local skill content",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return _error(str(exc))


def _resolve_skill(name: str, root: Path) -> dict[str, Any]:
    parts = _validate_relative(name, label="skill name")
    if len(parts) > 1:
        skill_dir = root.joinpath(*parts)
        _assert_local_nonsymlink(root, skill_dir)
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            raise WorkerSkillsError(f"local skill {name!r} was not found")
        return _metadata(skill_md, root)

    matches = [
        item
        for item in _iter_local_skills(root)
        if item["name"] == name or item["skill_md"].parent.name == name
    ]
    if not matches:
        raise WorkerSkillsError(f"local skill {name!r} was not found")
    if len(matches) != 1:
        raise WorkerSkillsError(
            f"local skill name {name!r} is ambiguous; use its categorized relative path"
        )
    return matches[0]


def _linked_files(root: Path, skill_dir: Path) -> dict[str, list[str]] | None:
    linked: dict[str, list[str]] = {}
    for section in _ALLOWED_SUPPORT_DIRS:
        support = skill_dir / section
        if support.is_symlink() or not support.is_dir():
            continue
        files: list[str] = []
        for directory, dirs, names in os.walk(support, followlinks=False):
            base = Path(directory)
            dirs[:] = sorted(d for d in dirs if not (base / d).is_symlink())
            for filename in sorted(names):
                path = base / filename
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    _assert_local_nonsymlink(root, path)
                except WorkerSkillsError:
                    continue
                files.append(path.relative_to(skill_dir).as_posix())
        if files:
            linked[section] = sorted(files)
    return linked or None


def worker_skill_view(name: str, file_path: str | None = None) -> str:
    """Load SKILL.md or one support file without preprocessing or mutation."""
    try:
        _, home = _active_profile_home()
        root = _skills_root(home)
        if root.is_symlink():
            raise WorkerSkillsError("profile skills directory must not be a symlink")
        if not root.is_dir():
            raise WorkerSkillsError("profile has no local skills directory")
        item = _resolve_skill(name, root)
        skill_md = item["skill_md"]
        skill_dir = skill_md.parent

        if file_path is not None:
            parts = _validate_relative(file_path, label="linked file path")
            if parts[0] not in _ALLOWED_SUPPORT_DIRS:
                raise WorkerSkillsError(
                    "linked files must be under references, templates, scripts, or assets"
                )
            target = skill_dir.joinpath(*parts)
            _assert_local_nonsymlink(root, target)
            if not target.exists():
                raise WorkerSkillsError(f"linked file {file_path!r} was not found")
            content = _read_regular_text(root, target, max_bytes=_MAX_LINKED_FILE_BYTES)
            return json.dumps(
                {
                    "success": True,
                    "name": item["name"],
                    "file": PurePosixPath(*parts).as_posix(),
                    "content": content,
                },
                ensure_ascii=False,
            )

        rel_path = skill_md.relative_to(root).as_posix()
        linked = _linked_files(root, skill_dir)
        return json.dumps(
            {
                "success": True,
                "name": item["name"],
                "description": item["description"],
                "content": item["content"],
                "path": rel_path,
                "linked_files": linked,
                "usage_hint": (
                    "Use worker_skill_view(name, file_path) for listed support files"
                    if linked
                    else None
                ),
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return _error(str(exc))


WORKER_SKILLS_LIST_SCHEMA = {
    "name": "worker_skills_list",
    "description": "List read-only skills from this confined maker profile's local skills tree.",
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": "Optional local category filter",
            }
        },
        "required": [],
    },
}

WORKER_SKILL_VIEW_SCHEMA = {
    "name": "worker_skill_view",
    "description": (
        "Read a local profile SKILL.md or a linked text file under "
        "references/templates/scripts/assets. This tool never mutates skill state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Local skill name or categorized path"},
            "file_path": {
                "type": "string",
                "description": "Optional listed support-file path",
            },
        },
        "required": ["name"],
    },
}

registry.register(
    name="worker_skills_list",
    toolset="worker_skills",
    schema=WORKER_SKILLS_LIST_SCHEMA,
    handler=lambda args, **_: worker_skills_list(category=args.get("category")),
    check_fn=check_worker_skills_requirements,
    emoji="📖",
)
registry.register(
    name="worker_skill_view",
    toolset="worker_skills",
    schema=WORKER_SKILL_VIEW_SCHEMA,
    handler=lambda args, **_: worker_skill_view(
        args.get("name", ""), file_path=args.get("file_path")
    ),
    check_fn=check_worker_skills_requirements,
    emoji="📖",
)
