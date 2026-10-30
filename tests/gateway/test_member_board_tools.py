"""Member pilot discovery and own-board routing with disposable native DBs."""
import json

import pytest

from gateway import member_board_tools as member, josh_pilot_tools as pilot
from gateway import josh_board_authority as authority
from hermes_cli import board_owner_manage as control, kanban_db as kb
from tools import kanban_tools as native


@pytest.fixture
def own_board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(control, "ROOT", home)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_TASK", "HERMES_SESSION_SOURCE", "HERMES_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    grant = {"principal": "new-member", "profile": "new-member", "board": "member-space", "fingerprint": "grant-v1"}
    monkeypatch.setattr(control, "require_owner", lambda: dict(grant) if grant else None)
    own_path = home / "kanban" / "boards" / "member-space" / "kanban.db"
    other_path = home / "kanban" / "boards" / "other-space" / "kanban.db"
    own_path.parent.mkdir(parents=True)
    other_path.parent.mkdir(parents=True)
    with kb.connect_closing(own_path) as conn:
        own_id = kb.create_task(conn, title="Own fixture", assignee="maker", triage=True)
    with kb.connect_closing(other_path) as conn:
        other_id = kb.create_task(conn, title="PRIVATE_OTHER_MEMBER_SENTINEL", assignee="maker", triage=True)

    def admit(board):
        if board not in (None, grant["board"]):
            raise PermissionError("cross-board")
        return grant["board"]

    def connect(*, board=None):
        assert admit(board) == "member-space"
        return kb, kb.connect(own_path)

    monkeypatch.setattr(authority, "admitted_board", admit)
    monkeypatch.setattr(native, "_connect", connect)
    monkeypatch.setattr(native, "_profile_has_kanban_toolset", lambda: False)
    yield grant, own_id, other_id


def test_show_reads_real_own_board_and_never_falls_back(own_board):
    _, own_id, other_id = own_board
    result = json.loads(member.show({"task_id": own_id}))
    assert result["task"]["id"] == own_id
    assert result["task"]["title"] == "Own fixture"
    assert result["instruction_recovery"]["retry_granted"] is False
    assert result["instruction_recovery"]["current_instruction"]["original_request_ref"] == "task.body"
    assert result["board_administration"]["native"]["tool"] == "own_work_manage"
    assert len(result["board_administration"]["native"]["expected_state"]) == 64
    assert "command_templates" not in result["board_administration"]
    unavailable = member.show({"task_id": other_id})
    assert "error" in json.loads(unavailable)
    assert "PRIVATE_OTHER_MEMBER_SENTINEL" not in unavailable


def test_list_reads_only_current_members_real_board(own_board, monkeypatch):
    _, own_id, other_id = own_board
    monkeypatch.setattr(pilot, "pilot_required", lambda: False)
    result = json.loads(member.list_work({"query": "Own fixture", "status": "triage", "limit": 1}))
    assert result["read_only"]
    assert [row["id"] for row in result["items"]] == [own_id]
    assert other_id not in json.dumps(result)
    assert not json.loads(member.list_work({"query": "PRIVATE_OTHER_MEMBER_SENTINEL"}))["items"]


def test_list_revocation_during_read_withholds_result(own_board, monkeypatch):
    grant, _, _ = own_board
    original = pilot._list_native
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        grant.clear()
        return result
    monkeypatch.setattr(pilot, "_list_native", revoke)
    with pytest.raises(PermissionError, match="own-board authority|Own-board authority"):
        member.list_work({})


@pytest.mark.parametrize("extra", [{"board": "other"}, {"principal": "chris"}, {"profile": "default"}, {"database": "/tmp/other.db"}])
def test_show_and_manage_reject_model_identity_or_board_override(own_board, monkeypatch, extra):
    _, own_id, _ = own_board
    monkeypatch.setattr(native, "_handle_manage", lambda *args, **kwargs: pytest.fail("control must not run"))
    with pytest.raises((ValueError, PermissionError)):
        member.show({"task_id": own_id, **extra})
    with pytest.raises((ValueError, PermissionError)):
        member.manage({"task_id": own_id, "action": "archive", **extra})
    with pytest.raises(ValueError):
        member.comment({"task_id": own_id, "body": "Actual user direction.", **extra})
    with pytest.raises((ValueError, PermissionError)):
        member.list_work(extra)


def test_manage_passes_only_current_authenticated_board_to_core(own_board, monkeypatch):
    grant, own_id, _ = own_board
    calls = []
    monkeypatch.setattr(native, "_handle_manage", lambda args: calls.append(args) or json.dumps({"ok": True}))
    args = {"task_id": own_id, "action": "archive", "reason": "obsolete fixture", "expected_state": "a" * 64}
    assert json.loads(member.manage(args))["ok"]
    assert calls == [dict(args, board=grant["board"])]
    assert "board" not in args


def test_authority_change_during_show_does_not_release_result(own_board, monkeypatch):
    grant, own_id, _ = own_board
    original = native._handle_show
    def revoke(args):
        result = original(args)
        grant.clear()
        return result
    monkeypatch.setattr(native, "_handle_show", revoke)
    with pytest.raises(PermissionError, match="own-board authority|Own-board authority"):
        member.show({"task_id": own_id})


def test_registered_pilot_discovery_tracks_current_grant(own_board, monkeypatch):
    import model_tools
    from gateway import owner_api_intake, owner_device_tools, owner_delegation
    from tools.registry import registry
    grant, _, _ = own_board
    monkeypatch.setattr(pilot, "pilot_required", lambda: True)
    monkeypatch.setattr(authority, "app_binding", lambda: None)
    monkeypatch.setattr(owner_api_intake, "active", lambda: False)
    monkeypatch.setattr(owner_device_tools, "enabled", lambda: False)
    monkeypatch.setattr(owner_delegation, "active", lambda: False)
    pilot.prepare()
    schemas = model_tools.get_tool_definitions([pilot.TOOLSET], quiet_mode=True, skip_tool_search_assembly=True)
    names = {item["function"]["name"] for item in schemas}
    assert names == {pilot.TOOL, pilot.LIST_TOOL, *member.TOOLS}
    manage = next(item["function"] for item in schemas if item["function"]["name"] == member.MANAGE_TOOL)
    assert "board" in manage["parameters"]["properties"]
    assert "Chris" in manage["parameters"]["properties"]["board"]["description"]
    assert "board" in native.KANBAN_MANAGE_SCHEMA["parameters"]["properties"]
    grant.clear()
    # The shared registry still contains the member handlers; current grant
    # loss must remove them from the next caller's schema and dispatch.
    assert {member.SHOW_TOOL, member.MANAGE_TOOL}.issubset(registry._tools)
    assert not member.enabled()
    assert not (member.TOOLS & pilot.allowed_names())
    schemas = model_tools.get_tool_definitions([pilot.TOOLSET], quiet_mode=True, skip_tool_search_assembly=True)
    assert {item["function"]["name"] for item in schemas} == {pilot.TOOL}


def test_worker_and_main_owner_do_not_receive_member_tools(monkeypatch):
    def worker():
        raise PermissionError("worker cannot become a member owner")
    monkeypatch.setattr(control, "require_owner", worker)
    assert not member.enabled()
    with pytest.raises(PermissionError):
        member.manage({"task_id": "t_fixture", "action": "archive"})
    with pytest.raises(PermissionError):
        member.comment({"task_id": "t_fixture", "body": "test"})
    with pytest.raises(PermissionError):
        member.list_work({})
    monkeypatch.setattr(control, "require_owner", lambda: None)
    assert not member.enabled()


def test_host_gate_allows_only_exact_current_member_pilot_grant(own_board, monkeypatch):
    from gateway import host_tool_authority as host
    monkeypatch.setattr(pilot, "pilot_required", lambda: True)
    monkeypatch.setattr(pilot, "allowed_names", lambda: set(member.TOOLS))
    token = authority._execution.set(("new-member", "new-member"))
    try:
        assert all(host.tool_allowed(name) for name in member.TOOLS)
        assert not host.tool_allowed("kanban_manage")
    finally:
        authority._execution.reset(token)
    token = authority._execution.set(("new-member", "another-member"))
    try:
        # Authenticated enrollment validation rejects the mismatched identity;
        # the host gate consumes that current result, not old schemas.
        monkeypatch.setattr(member, "enabled", lambda: False)
        assert all(not host.tool_allowed(name) for name in member.TOOLS)
    finally:
        authority._execution.reset(token)


def test_comment_is_account_authored_and_own_card_only(own_board, monkeypatch):
    grant, own_id, other_id = own_board
    monkeypatch.setenv("HERMES_PROFILE", "default")
    body = "The user asked to continue this same scoped task; the missing tool is now installed."
    result = json.loads(member.comment({"task_id": own_id, "body": body}))
    assert result["ok"]
    shown = json.loads(member.show({"task_id": own_id}))
    saved = next(c for c in shown["comments"] if c["id"] == result["comment_id"])
    assert saved["author"] == grant["profile"]
    assert saved["body"] == body
    assert shown["board_administration"]["native"]["expected_latest_comment_id"] == result["comment_id"]
    with pytest.raises(ValueError):
        member.comment({"task_id": own_id, "body": body, "author": "chris"})
    assert "error" in json.loads(member.comment({"task_id": other_id, "body": body}))


def test_database_override_is_rejected_before_core_read(own_board, monkeypatch, tmp_path):
    _, own_id, _ = own_board
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "elsewhere.db"))
    monkeypatch.setattr(native, "_handle_show", lambda *args: pytest.fail("override must fail before read"))
    monkeypatch.setattr(pilot, "_list_native", lambda *args, **kwargs: pytest.fail("override must fail before list"))
    with pytest.raises(PermissionError, match="database override"):
        member.show({"task_id": own_id})
    with pytest.raises(PermissionError, match="database override"):
        member.list_work({})


def test_normal_kanban_catalog_exposes_aliases_without_requiring_pilot(own_board, monkeypatch):
    import model_tools
    monkeypatch.setattr(pilot, "pilot_required", lambda: False)
    monkeypatch.setattr(authority, "app_binding", lambda: None)
    token = authority._execution.set(("new-member", "new-member"))
    try:
        schemas = model_tools.get_tool_definitions(["kanban"], quiet_mode=False, skip_tool_search_assembly=True)
        names = {item["function"]["name"] for item in schemas}
        assert member.TOOLS.issubset(names)
        assert "kanban_manage" not in names
    finally:
        authority._execution.reset(token)


@pytest.mark.parametrize(("principal", "use_pilot"), [("connor", False), ("future-member", True)])
def test_generic_delegation_catalog_is_loaded_and_currently_authorized(own_board, monkeypatch, principal, use_pilot):
    import model_tools
    from gateway import owner_delegation as work, owner_api_intake, owner_device_tools
    from gateway import host_tool_authority as host
    monkeypatch.setattr(pilot, "pilot_required", lambda: use_pilot)
    monkeypatch.setattr(authority, "app_binding", lambda: None)
    monkeypatch.setattr(owner_api_intake, "active", lambda: False)
    monkeypatch.setattr(owner_device_tools, "enabled", lambda: False)
    enrolled = {"owner": principal, "profile": principal, "board": "member-space", "fingerprint": "current"}
    monkeypatch.setattr(work, "enrollment", lambda owner: dict(enrolled))
    execution = authority._execution.set((principal, principal))
    token = work._ACTIVE.set({"principal": principal, "authority": dict(enrolled), "revalidate": None})
    try:
        if use_pilot:
            pilot.prepare()
        tools = [pilot.TOOLSET] if use_pilot else ["kanban"]
        schemas = model_tools.get_tool_definitions(tools, quiet_mode=False, skip_tool_search_assembly=True)
        names = {item["function"]["name"] for item in schemas}
        assert work.TOOLS.issubset(names)
        assert all(host.tool_allowed(name) for name in work.TOOLS)
        enrolled["fingerprint"] = "revoked"
        assert not work.available()
        assert all(not host.tool_allowed(name) for name in work.TOOLS)
        if use_pilot:
            # The captured pilot expectation still includes delegation while
            # active(): absence after revocation is an intentional closed gate.
            with pytest.raises(PermissionError, match="native owner tools"):
                model_tools.get_tool_definitions(tools, quiet_mode=False, skip_tool_search_assembly=True)
        else:
            schemas = model_tools.get_tool_definitions(tools, quiet_mode=False, skip_tool_search_assembly=True)
            assert not (work.TOOLS & {item["function"]["name"] for item in schemas})
    finally:
        work._ACTIVE.reset(token)
        authority._execution.reset(execution)
