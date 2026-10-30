"""Read-only Margot Accounts vault backend (t_23001aff)."""

import json

from agent.vault_backends.accounts import AccountsLoginBackend

SECRET = "s3cr3t-Pa55-xyz"


def _vault(tmp_path):
    (tmp_path / "apple").mkdir()
    (tmp_path / "apple" / "apple-developer-gus-admin.json").write_text(json.dumps(
        {"data": {"email": "admin@example.com", "custom:Password": SECRET}, "deleted": False}))
    (tmp_path / "brave-api-key.txt").write_text("APIKEY-should-not-list")
    (tmp_path / "gcp-service-account.json").write_text(json.dumps({"private_key": "PK", "client_email": "x@y"}))
    (tmp_path / "people").mkdir()
    (tmp_path / "people" / "connor").mkdir()
    (tmp_path / "people" / "connor" / "x-login.json").write_text(json.dumps(
        {"service_url": "https://x.com", "password": "other-person"}))
    return AccountsLoginBackend({"root": str(tmp_path), "key_file": str(tmp_path / "nokey")})


def test_listing_has_apple_login_and_no_secret_values(tmp_path):
    items = _vault(tmp_path).list_items()
    assert len(items) == 1
    m = items[0]
    assert m.kind == "login" and m.identifier == "admin@example.com"
    assert "https://appstoreconnect.apple.com" in m.allowed_origins
    assert "https://idmsa.apple.com" in m.allowed_origins
    dumped = json.dumps([i.to_dict() for i in items]) + repr(items)
    assert SECRET not in dumped and "APIKEY" not in dumped and "other-person" not in dumped


def test_non_login_files_ignored(tmp_path):
    labels = [m.label for m in _vault(tmp_path).list_items()]
    assert not any("brave" in l or "gcp" in l or "x-login" in l for l in labels)


def test_resolve_password_server_side(tmp_path):
    b = _vault(tmp_path)
    h = b.list_items()[0].id
    assert h.startswith("acct:")
    assert b.resolve_password(h) == SECRET
    assert b.resolve_password("acct:nope") == ""


def test_fill_refuses_origin_mismatch(tmp_path, monkeypatch):
    import tools.browser_vault_tool as t
    b = _vault(tmp_path)
    h = b.list_items()[0].id
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda handle: b)
    monkeypatch.setattr(t, "_focus_bound_origin", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(t, "_current_page_origin", lambda *a, **k: "https://evil.example.com")
    called = []
    monkeypatch.setattr(b, "resolve_password", lambda handle: called.append(1) or SECRET)
    out = json.loads(t.browser_vault_fill(h))
    assert out["success"] is False and out["error_type"] == "origin_mismatch"
    assert SECRET not in json.dumps(out) and not called
