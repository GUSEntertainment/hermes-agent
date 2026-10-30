"""Security tests for the maker-only read-only skills toolset."""

import json
import os
from pathlib import Path

import pytest


def _admission(home: Path, profile: str = "terra-maker-alpha") -> dict:
    board = home.parent.parent / "board.db"
    board.touch(exist_ok=True)
    info = board.stat()
    return {
        "version": "hermes.worker-admission/v1", "task": "t_confined",
        "admission_nonce": "worker-skills-test-admission",
        "board_database": {
            "path": os.path.realpath(board), "device": info.st_dev,
            "inode": info.st_ino,
        },
        "profile": profile, "profile_home": str(home),
        "profile_config_digest": "a" * 64, "provider": "test", "model": "test",
        "fallbacks": [], "reasoning": None, "toolsets": ["skills"], "skills": [],
        "skill_dependencies": {"loaded": [], "unavailable": []}, "privacy": {},
        "tenant": None,
        "workspace": {"kind": "dir", "path": str(home.parent.parent), "branch": None, "project": None},
        "workspace_confinement": True,
        "workspace_read_only_roots": [],
        "workspace_read_only_root_identities": [],
    }


@pytest.fixture
def worker_skills(monkeypatch, tmp_path):
    import tools.worker_skills_tool as ws
    import tools.workspace_confinement as confinement

    home = tmp_path / "profiles" / "terra-maker-alpha"
    home.mkdir(parents=True)
    from agent import private_worker_mode
    private_worker_mode._reset_private_worker_contract_for_tests()
    private_worker_mode.install_authenticated_worker_contract(
        _admission(home), durable_validator=lambda _contract, _digest: True
    )
    monkeypatch.setattr(
        confinement,
        "_load_security_config",
        lambda: {"security": {"workspace_confinement": {"enabled": True}}},
    )
    monkeypatch.setattr(confinement, "_active_profile_name", lambda: "terra-maker-alpha")
    monkeypatch.setattr(ws, "get_hermes_home", lambda: home)
    yield ws, home
    private_worker_mode._reset_private_worker_contract_for_tests()


def _write_skill(home: Path, relative: str = "research/example") -> Path:
    skill_dir = home / "skills" / relative
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "templates").mkdir()
    (skill_dir / "scripts").mkdir()
    (skill_dir / "assets").mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: example\ndescription: Safe local example\n---\n# Example\nBody\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "guide.md").write_text("guide", encoding="utf-8")
    (skill_dir / "templates" / "sample.txt").write_text("sample", encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text("print('ok')", encoding="utf-8")
    (skill_dir / "assets" / "data.json").write_text("{}", encoding="utf-8")
    (skill_dir / "private.txt").write_text("never exposed", encoding="utf-8")
    return skill_dir


def _payload(raw: str) -> dict:
    return json.loads(raw)


def test_toolset_registers_only_read_only_worker_functions():
    import tools.skill_manager_tool  # noqa: F401
    import tools.worker_skills_tool  # noqa: F401
    from tools.registry import registry

    assert registry.get_tool_names_for_toolset("worker_skills") == [
        "worker_skill_view",
        "worker_skills_list",
    ]
    assert registry.get_toolset_for_tool("skill_manage") == "skills"
    assert "skill_manage" not in registry.get_tool_names_for_toolset("worker_skills")


@pytest.mark.parametrize("config", [{}, {"security": {}}, {"security": {"workspace_confinement": {"enabled": False}}}])
def test_tools_fail_closed_without_authenticated_admission_even_with_forged_env(
    worker_skills, monkeypatch, config
):
    ws, _ = worker_skills
    from agent import private_worker_mode
    private_worker_mode._reset_private_worker_contract_for_tests()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_confined")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/tmp")
    monkeypatch.setenv("HERMES_KANBAN_HANDOFF_PRESENT", "1")
    assert ws.check_worker_skills_requirements() is False
    assert _payload(ws.worker_skills_list())["success"] is False
    assert _payload(ws.worker_skill_view("example"))["success"] is False


@pytest.mark.parametrize("profile", [None, "", "default", "custom", "../terra", "terra/maker"])
def test_tools_fail_closed_for_unadmitted_profiles(worker_skills, monkeypatch, profile):
    ws, _ = worker_skills
    from agent import private_worker_mode
    private_worker_mode._reset_private_worker_contract_for_tests()
    monkeypatch.setenv("HERMES_PROFILE", str(profile or ""))
    assert ws.check_worker_skills_requirements() is False
    assert _payload(ws.worker_skills_list())["success"] is False


def test_tools_reject_hermes_home_not_bound_to_active_named_profile(worker_skills, monkeypatch, tmp_path):
    ws, _ = worker_skills
    wrong = tmp_path / "profiles" / "different-maker"
    wrong.mkdir(parents=True)
    monkeypatch.setattr(ws, "get_hermes_home", lambda: wrong)

    assert ws.check_worker_skills_requirements() is False
    assert "admitted profile" in _payload(ws.worker_skills_list())["error"]


def test_tools_fail_closed_when_named_profile_home_does_not_exist(worker_skills, monkeypatch, tmp_path):
    ws, _ = worker_skills
    missing = tmp_path / "missing-root" / "profiles" / "terra-maker-alpha"
    monkeypatch.setattr(ws, "get_hermes_home", lambda: missing)

    assert ws.check_worker_skills_requirements() is False
    assert _payload(ws.worker_skills_list())["success"] is False


def test_list_reads_only_local_profile_skills_and_supports_safe_category_filter(worker_skills, tmp_path):
    ws, home = worker_skills
    _write_skill(home)
    external = tmp_path / "external" / "foreign"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("---\nname: foreign\n---\nforeign")

    result = _payload(ws.worker_skills_list(category="research"))

    assert result["success"] is True
    assert result["count"] == 1
    assert result["skills"] == [
        {"name": "example", "description": "Safe local example", "category": "research"}
    ]
    assert "foreign" not in json.dumps(result)


@pytest.mark.parametrize("category", ["../research", "/research", "research/../../x", "plugin:research", r"C:\research"])
def test_list_rejects_category_escape(worker_skills, category):
    ws, _ = worker_skills
    result = _payload(ws.worker_skills_list(category=category))
    assert result["success"] is False
    assert "category" in result["error"].lower()


@pytest.mark.parametrize(
    "name",
    ["../example", "/tmp/example", "research/../../example", "plugin:example", r"C:\skills\example"],
)
def test_view_rejects_plugin_absolute_and_traversal_names(worker_skills, name):
    ws, home = worker_skills
    _write_skill(home)
    result = _payload(ws.worker_skill_view(name))
    assert result["success"] is False


@pytest.mark.parametrize(
    "file_path",
    ["../secret", "/etc/passwd", "private.txt", "SKILL.md", "references/../../private.txt", r"C:\secret"],
)
def test_view_allows_main_skill_or_only_linked_support_paths(worker_skills, file_path):
    ws, home = worker_skills
    _write_skill(home)
    result = _payload(ws.worker_skill_view("example", file_path=file_path))
    assert result["success"] is False


def test_view_returns_main_content_and_only_allowed_linked_files(worker_skills):
    ws, home = worker_skills
    _write_skill(home)

    result = _payload(ws.worker_skill_view("example"))

    assert result["success"] is True
    assert result["content"].endswith("# Example\nBody\n")
    assert result["path"] == "research/example/SKILL.md"
    assert result["linked_files"] == {
        "assets": ["assets/data.json"],
        "references": ["references/guide.md"],
        "scripts": ["scripts/run.py"],
        "templates": ["templates/sample.txt"],
    }
    assert "private.txt" not in json.dumps(result)
    linked = _payload(ws.worker_skill_view("research/example", "references/guide.md"))
    assert linked["success"] is True
    assert linked["content"] == "guide"


def test_symlinked_skills_and_linked_files_are_rejected(worker_skills, tmp_path):
    ws, home = worker_skills
    skill_dir = _write_skill(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: stolen\n---\nstolen")
    (home / "skills" / "linked-skill").symlink_to(outside, target_is_directory=True)
    (skill_dir / "references" / "leak.md").symlink_to(outside / "SKILL.md")

    listed = _payload(ws.worker_skills_list())
    assert [item["name"] for item in listed["skills"]] == ["example"]
    assert "leak.md" not in json.dumps(_payload(ws.worker_skill_view("example")))
    assert _payload(ws.worker_skill_view("linked-skill"))["success"] is False
    leaked = _payload(ws.worker_skill_view("example", "references/leak.md"))
    assert leaked["success"] is False
    assert "symlink" in leaked["error"].lower()


def test_symlinked_skills_root_fails_closed(worker_skills, tmp_path):
    ws, home = worker_skills
    outside = tmp_path / "outside-skills"
    outside.mkdir()
    (home / "skills").symlink_to(outside, target_is_directory=True)

    result = _payload(ws.worker_skills_list())
    assert result["success"] is False
    assert "symlink" in result["error"].lower()


def test_read_operations_create_no_directories_usage_counters_or_snapshots(worker_skills):
    ws, home = worker_skills
    assert not (home / "skills").exists()
    before = sorted(str(p.relative_to(home)) for p in home.rglob("*"))

    assert _payload(ws.worker_skills_list())["success"] is True

    after = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert after == before
    assert not (home / "skills").exists()
    assert not (home / ".skills_prompt_snapshot.json").exists()
    assert not (home / "skills" / ".usage.json").exists()


def test_model_toolset_resolution_exposes_only_read_only_worker_skills(worker_skills):
    _ws, _home = worker_skills
    import tools.worker_skills_tool  # noqa: F401
    import model_tools

    model_tools._clear_tool_defs_cache()
    defs = model_tools.get_tool_definitions(
        enabled_toolsets=["worker_skills"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {item["function"]["name"] for item in defs}
    assert names == {"worker_skills_list", "worker_skill_view"}
    assert "skill_manage" not in names


def test_confined_worker_replaces_saved_legacy_skills_at_model_boundary(
    worker_skills, monkeypatch
):
    _ws, _home = worker_skills
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_confined")
    monkeypatch.setenv("HERMES_KANBAN_HANDOFF_PRESENT", "1")
    import tools.worker_skills_tool  # noqa: F401
    from tools.registry import invalidate_check_fn_cache
    import model_tools

    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()
    defs = model_tools.get_tool_definitions(
        enabled_toolsets=["skills"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    names = {item["function"]["name"] for item in defs}

    assert {"worker_skills_list", "worker_skill_view"} <= names
    assert not {"skills_list", "skill_view", "skill_manage"} & names
    # HERMES_KANBAN_TASK intentionally auto-adds lifecycle tools; no unrelated
    # profile toolset may leak through the saved `skills` selection.
    from tools.registry import registry

    assert names - {"worker_skills_list", "worker_skill_view"} <= set(
        registry.get_tool_names_for_toolset("kanban")
    )


@pytest.mark.parametrize("legacy_name", ["skills_list", "skill_view", "skill_manage"])
def test_confined_worker_cannot_invoke_legacy_skill_handlers(
    worker_skills, monkeypatch, legacy_name
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_confined")
    monkeypatch.setenv("HERMES_KANBAN_HANDOFF_PRESENT", "1")
    import model_tools

    result = _payload(
        model_tools.handle_function_call(
            legacy_name,
            {},
            task_id="t_confined",
            skip_pre_tool_call_hook=True,
            skip_tool_request_middleware=True,
            skip_tool_execution_middleware=True,
        )
    )

    assert result["error"]
    assert "workspace-confined" in result["error"]


@pytest.mark.parametrize("order", [("sol-maker", "terra-maker"), ("terra-maker", "sol-maker")])
def test_assigned_profile_skill_boundary_has_no_sol_terra_order_bleed(
    tmp_path, monkeypatch, order
):
    from agent import private_worker_mode
    import model_tools
    import tools.worker_skills_tool as ws
    from tools.registry import invalidate_check_fn_cache

    observed = []
    for profile in order:
        home = tmp_path / "profiles" / profile
        home.mkdir(parents=True, exist_ok=True)
        private_worker_mode._reset_private_worker_contract_for_tests()
        private_worker_mode.install_authenticated_worker_contract(
            _admission(home, profile=profile),
            durable_validator=lambda _contract, _digest: True,
        )
        monkeypatch.setattr(ws, "get_hermes_home", lambda home=home: home)
        invalidate_check_fn_cache()
        model_tools._clear_tool_defs_cache()
        names = {
            item["function"]["name"]
            for item in model_tools.get_tool_definitions(
                enabled_toolsets=["skills"], quiet_mode=True,
                skip_tool_search_assembly=True,
            )
        }
        observed.append(names)
    assert all({"worker_skills_list", "worker_skill_view"} <= names for names in observed)
    assert all(not {"skills_list", "skill_view", "skill_manage"} & names for names in observed)
