import base64
import json
import time
from pathlib import Path

import pytest

from agent import auxiliary_client as aux
from hermes_cli import auth


def token(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJub25lIn0." + payload + ".fixture"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    cli = tmp_path / "codex"
    home.mkdir()
    cli.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(cli))
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: None)
    monkeypatch.setattr(aux, "_select_pool_entry", lambda provider: (False, None))
    return home, cli


def save_auth(home, tokens):
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {"openai-codex": {"tokens": tokens}}}))


def save_cli(cli, access):
    p = cli / "auth.json"
    p.write_text(json.dumps({"tokens": {"access_token": access, "refresh_token": "fixture-new-refresh"}}))
    return p.read_bytes()


def test_cleared_singleton_recovers_valid_cli_login(homes):
    home, cli = homes
    save_auth(home, {})
    access = token(time.time() + 3600)
    original = save_cli(cli, access)
    assert aux._read_codex_access_token() == access
    saved = json.loads((home / "auth.json").read_text())
    assert saved["providers"]["openai-codex"]["tokens"]["access_token"] == access
    assert (cli / "auth.json").read_bytes() == original


def test_expired_token_refresh_uses_existing_runtime(homes, monkeypatch):
    home, cli = homes
    save_auth(home, {"access_token": token(time.time() - 10), "refresh_token": "fixture-old-refresh"})
    access = token(time.time() + 3600)
    calls = []
    def refresh(*args, **kwargs):
        calls.append(1)
        return {"access_token": access, "refresh_token": "fixture-refreshed"}
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", refresh)
    assert aux._read_codex_access_token() == access
    assert calls == [1]


def test_rotated_refresh_recovers_cli_without_rewriting_cli(homes, monkeypatch):
    home, cli = homes
    save_auth(home, {"access_token": token(time.time() - 10), "refresh_token": "fixture-used-refresh"})
    access = token(time.time() + 3600)
    original = save_cli(cli, access)
    def reused(*args, **kwargs):
        raise auth.AuthError("fixture reused token", provider="openai-codex", code="refresh_token_reused", relogin_required=True)
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", reused)
    assert aux._read_codex_access_token() == access
    assert (cli / "auth.json").read_bytes() == original


def test_valid_store_does_not_refresh(homes, monkeypatch):
    home, cli = homes
    access = token(time.time() + 3600)
    save_auth(home, {"access_token": access, "refresh_token": "fixture-refresh"})
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", lambda *a, **k: pytest.fail("unexpected refresh"))
    assert aux._read_codex_access_token() == access


def test_unavailable_login_returns_none_without_inference(homes):
    home, cli = homes
    save_auth(home, {})
    assert aux._read_codex_access_token() is None


def test_pool_choice_stays_first(homes, monkeypatch):
    monkeypatch.setattr(aux, "_select_pool_entry", lambda provider: (True, object()))
    monkeypatch.setattr(aux, "_pool_runtime_api_key", lambda entry: "fixture-pool-token")
    monkeypatch.setattr(auth, "resolve_codex_runtime_credentials", lambda: pytest.fail("pool should win"))
    assert aux._read_codex_access_token() == "fixture-pool-token"


def test_active_profile_recovery_writes_only_active_home(homes, monkeypatch):
    root, cli = homes
    root_before = b'{"version":1,"providers":{}}'
    (root / "auth.json").write_bytes(root_before)
    member = root / "profiles" / "fixture-member"
    member.mkdir(parents=True)
    save_auth(member, {})
    access = token(time.time() + 3600)
    save_cli(cli, access)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    handle = set_hermes_home_override(str(member))
    try:
        assert aux._read_codex_access_token() == access
    finally:
        reset_hermes_home_override(handle)
    assert (root / "auth.json").read_bytes() == root_before
    assert json.loads((member / "auth.json").read_text())["providers"]["openai-codex"]["tokens"]["access_token"] == access


def test_failed_refresh_does_not_return_expired_token(homes, monkeypatch):
    home, cli = homes
    save_auth(home, {"access_token": token(time.time() - 10), "refresh_token": "fixture-refresh"})
    def failed(*args, **kwargs):
        raise auth.AuthError("fixture refresh failed", provider="openai-codex", code="refresh_failed", relogin_required=True)
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", failed)
    assert aux._read_codex_access_token() is None


def test_unavailable_oauth_does_not_request_fictitious_api_key(homes, monkeypatch):
    monkeypatch.setattr(aux, "_get_cached_client", lambda *a, **k: (None, None))
    monkeypatch.setattr(aux, "_try_configured_fallback_for_unavailable_client", lambda *a, **k: (None, None, None))
    with pytest.raises(RuntimeError, match="Codex OAuth login") as exc:
        aux.call_llm(provider="openai-codex", model="fixture-model", messages=[{"role":"user","content":"fixture"}])
    assert "OPENAI-CODEX_API_KEY" not in str(exc.value)
