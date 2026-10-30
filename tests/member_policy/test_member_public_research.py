from gateway import member_worker_policy as policy


def test_declared_web_survives_private_workspace_overlay():
    config = {"toolsets": ["web", "browser", "terminal"],
              "mcp_servers": {"mail": {}}, "agent": {"max_turns": 12}}
    result = policy.effective(config, {"version": policy.PRIVATE_VERSION})
    assert "web" in result["toolsets"]
    assert "browser" not in result["toolsets"]
    assert result["mcp_servers"] == {}
    assert result["agent"]["max_turns"] == 12
    assert result["security"]["workspace_confinement"]["deny_terminal_network"] is True
    assert result["security"]["workspace_confinement"]["deny_home_reads_outside_workspace"] is True
    assert config["mcp_servers"] == {"mail": {}}


def test_no_web_grant_without_installed_profile_declaration():
    assert "web" not in policy.effective({"toolsets": ["terminal"]},
        {"version": policy.PRIVATE_VERSION})["toolsets"]
    assert "web" not in policy.effective({"toolsets": ["web"],
        "agent": {"disabled_toolsets": ["web"]}},
        {"version": policy.PRIVATE_VERSION})["toolsets"]


def test_cli_platform_web_override_respected():
    result = policy.effective({"toolsets": ["web"],
        "platform_toolsets": {"cli": ["terminal"]}},
        {"version": policy.PRIVATE_VERSION})
    assert "web" not in result["toolsets"]


def test_admitted_public_research_catalog_and_cross_task_denial(monkeypatch):
    from agent import private_worker_mode, delegation_context
    contract = {"member_company_policy": {"version": policy.PRIVATE_VERSION},
                "toolsets": [*policy.TOOLSETS, "web"], "workspace_confinement": True}
    monkeypatch.setattr(private_worker_mode, "get_authenticated_worker_contract", lambda: contract)
    monkeypatch.setattr(policy, "verify", lambda value: None)
    monkeypatch.setattr(delegation_context, "is_delegated_child_context", lambda: False)
    assert policy.allowed_tool("web_search") and policy.allowed_tool("web_extract")
    assert not policy.allowed_tool("browser_navigate")
    assert not policy.allowed_tool("mcp_mail_read")
    monkeypatch.setattr(policy, "authenticated_private_policy", lambda: {
        "version": policy.PRIVATE_VERSION, "task": "t_own", "board": "member-home"})
    import pytest
    with pytest.raises(PermissionError, match="outside admitted scope"):
        policy.authorize_private_kanban("kanban_show", {"task_id": "t_other"})


def test_owner_delegation_delivery_is_not_phone_action_authority():
    from gateway.private_action_worker_policy import delivery_instructions, VERSION
    def incorrect_verifier():
        raise AssertionError("An owner-delegated action must not use phone task link admission")
    assert delivery_instructions(None, {"version": VERSION, "task": "t_own",
        "action": "owner_work:t_own"}, "t_own", verify=incorrect_verifier) is None


def test_private_startup_uses_declared_catalog_not_ambient_cli_tools(monkeypatch):
    from hermes_cli import tools_config
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *args,**kwargs:
        {"file", "kanban", "phone", "google_meet", "shared_history"})
    private={"version": policy.PRIVATE_VERSION}
    config=policy.effective({"toolsets":["web"]},private)
    tools=policy.admission_toolsets(config,private)
    assert tools == set(policy.TOOLSETS) | {"web"}
    assert not tools & {"phone", "google_meet", "shared_history"}
    assert "phone" in policy.admission_toolsets(config,None)
    config["agent"]={"disabled_toolsets":["delegation"]}
    assert "delegation" not in policy.admission_toolsets(config,private)
