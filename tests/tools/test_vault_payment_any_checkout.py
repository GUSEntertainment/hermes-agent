"""t_78bb3d31: saved payment cards usable on any Chris-approved checkout, confirmation still required."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.vault_store import VaultError, VaultStore

_CARD = {"card_number": "4242424242424242", "cardholder_name": "T", "exp_month": "07", "exp_year": "2029",
         "cvc": "123", "billing_postal_code": "10001"}


@pytest.fixture()
def stores(tmp_path):
    root = VaultStore(tmp_path / "root" / "vault")
    prof = VaultStore(tmp_path / "root" / "profiles" / "w" / "vault", share_payments=True)
    yield root, prof


def test_profile_vault_sees_root_payment_cards_but_not_root_logins(stores):
    root, prof = stores
    card = root.add_item("payment", "GUS card ****4242", _CARD, origin="https://booking.a.test")
    root.add_item("login", "x", {"identifier_type": "email", "identifier": "a@b", "password": "pw"},
                  origin="https://a.test")
    ids = {m.id: m.kind for m in prof.list_items()}
    assert ids == {card.id: "payment"}
    assert prof.get_meta(card.id).kind == "payment"
    assert prof.resolve_secret(card.id)["cvc"] == "123"


def test_resave_same_card_dedupes_and_records_origin(stores):
    root, _ = stores
    a = root.add_item("payment", "c1", _CARD, origin="https://a.test")
    b = root.add_item("payment", "c2", dict(_CARD, card_number="4242 4242 4242 4242"), origin="https://b.test")
    assert a.id == b.id and len(root.list_items()) == 1
    assert root.get_meta(a.id).allowed_origins == ("https://a.test", "https://b.test")


def test_dedupe_payments_merges_existing_copies(stores):
    root, _ = stores
    # Simulate legacy duplicates written before the dedupe-on-save rule.
    with root._locked():
        root._write_all([
            {"id": "vault_old", "kind": "payment", "label": "old", "origin": "https://a.test",
             "created_at": "2026-01-01", "secret": dict(_CARD)},
            {"id": "vault_new", "kind": "payment", "label": "new", "origin": "https://b.test",
             "created_at": "2026-02-01", "secret": dict(_CARD)},
        ])
    res = root.dedupe_payments()
    assert res == {"removed": ["vault_new"], "kept": ["vault_old"]}
    assert "4242" not in json.dumps(res)
    assert root.get_meta("vault_old").allowed_origins == ("https://a.test", "https://b.test")


def test_allow_refuses_login_items(stores):
    root, _ = stores
    lg = root.add_item("login", "x", {"identifier_type": "email", "identifier": "a@b", "password": "pw"},
                       origin="https://a.test")
    with pytest.raises(VaultError):
        root.allow_payment_origin(lg.id, "https://evil.test")
    assert root.get_meta(lg.id).allowed_origins == ()


def test_list_shows_other_site_card_and_fill_still_requires_confirmation(stores):
    from tools import browser_vault_tool

    root, _ = stores
    card = root.add_item("payment", "GUS card", _CARD, origin="https://booking.a.test")
    with patch("agent.vault_store.get_vault_store", return_value=root), \
         patch("agent.vault_backends.base._cfg", return_value={"accounts": {"enabled": False}}), \
         patch("agent.vault_backends.base.external_backend_classes", return_value=()):
        listed = json.loads(browser_vault_tool.browser_vault_list())
        entry = next(i for i in listed["items"] if i["handle"] == card.id)
        assert entry["status"] == "saved, other site" and entry["available"] is True
        assert "4242" not in json.dumps(listed)

        def fake_eval(task_id, expression):
            if "location.href" in expression:
                return {"success": True, "result": "https://www.paynearme.test/pay"}
            return {"success": True, "result": json.dumps([{"autocomplete": "cc-number", "index": 0, "type": "text"}])}

        written = []
        with patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret",
                          side_effect=lambda t, e: written.append(e) or {"success": True, "result": json.dumps({"filled": 1})}), \
             patch("tools.approval_prompt.request_elicitation_consent", return_value="decline") as consent:
            out = json.loads(browser_vault_tool.browser_vault_fill(card.id))
        assert out["error_type"] == "payment_declined" and written == [] and consent.call_count == 1
        assert "https://www.paynearme.test" not in root.get_meta(card.id).allowed_origins

        with patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
             patch.object(browser_vault_tool, "_eval_js_secret",
                          side_effect=lambda t, e: written.append(e) or {"success": True, "result": json.dumps({"filled": 1})}), \
             patch("tools.approval_prompt.request_elicitation_consent", return_value="accept") as consent:
            raw = browser_vault_tool.browser_vault_fill(card.id)
        assert json.loads(raw)["success"] is True and consent.call_count == 1 and len(written) == 1
        assert "4242424242424242" not in raw
        assert "https://www.paynearme.test" in root.get_meta(card.id).allowed_origins


# -- Grace review regressions (comment 11222) ---------------------------------

def test_contextual_store_shares_only_with_its_own_root(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    root_a, root_b = tmp_path / "A", tmp_path / "B"
    (root_a / "profiles" / "worker").mkdir(parents=True)
    (root_b / "profiles" / "w2").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root_a / "profiles" / "worker"))
    card = VaultStore(root_a / "vault").add_item("payment", "c", _CARD)
    # Positive: worker profile of root A sees root A's card.
    assert card.id in {m.id for m in VaultStore().list_items()}
    # Negative: an unrelated contextual home (root B, and a root-B profile) never sees it.
    for home in (root_b, root_b / "profiles" / "w2"):
        tok = set_hermes_home_override(home)
        try:
            st = VaultStore()
            assert card.id not in {m.id for m in st.list_items()}
            with pytest.raises(VaultError):
                st.resolve_secret(card.id)
        finally:
            reset_hermes_home_override(tok)


def test_explicit_standalone_store_is_isolated(tmp_path):
    root = VaultStore(tmp_path / "r" / "vault")
    card = root.add_item("payment", "c", _CARD)
    alone = VaultStore(tmp_path / "r" / "profiles" / "x" / "vault")
    assert alone.list_items() == []
    with pytest.raises(VaultError):
        alone.resolve_secret(card.id)


def test_resave_with_corrected_fields_updates_canonical_record(stores):
    root, _ = stores
    a = root.add_item("payment", "c", _CARD, origin="https://a.test")
    b = root.add_item("payment", "c", dict(_CARD, cvc="999", billing_postal_code="02139"),
                      origin="https://a.test")
    assert a.id == b.id
    sec = root.resolve_secret(b.id)
    assert sec["cvc"] == "999" and sec["billing_postal_code"] == "02139"
    assert len(root.list_items()) == 1


def test_profile_resave_merges_into_root_card(stores):
    root, prof = stores
    a = root.add_item("payment", "c", _CARD, origin="https://a.test")
    b = prof.add_item("payment", "c", dict(_CARD, cvc="777"), origin="https://b.test")
    assert b.id == a.id
    assert [m.id for m in prof.list_items()] == [a.id]
    with prof._locked():
        assert prof._read_all() == []  # nothing stored profile-locally
    assert root.resolve_secret(a.id)["cvc"] == "777"
    assert root.get_meta(a.id).allowed_origins == ("https://a.test", "https://b.test")


def test_concurrent_saves_of_same_card_persist_one_record(tmp_path):
    """Real cross-process race: several interpreters released together save the same card."""
    import os, subprocess, sys, time
    from pathlib import Path
    import agent.vault_store as vs
    src = str(Path(vs.__file__).resolve().parents[1])
    base, go = tmp_path / "solo" / "vault", tmp_path / "go"
    code = ("import os,sys,time\nfrom agent.vault_store import VaultStore\n"
            "while not os.path.exists(sys.argv[2]): time.sleep(0.001)\n"
            "c={'card_number':'4242424242424242','cardholder_name':'T','exp_month':'07','exp_year':'2029','cvc':sys.argv[3]}\n"
            "print(VaultStore(sys.argv[1]).add_item('payment','c',c).id)")
    env = dict(os.environ, PYTHONPATH=src, PYTHONDONTWRITEBYTECODE="1")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(base), str(go), str(100 + n)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for n in range(6)]
    time.sleep(1.5); go.touch()
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), [e[-300:] for _, e in outs]
    assert len({o.strip() for o, _ in outs}) == 1
    assert len(VaultStore(base).list_items()) == 1


def test_concurrent_threads_cannot_interleave_check_and_append(stores):
    """Deterministic: a second save started while the first sits between check and append waits."""
    import threading
    root, _ = stores
    gate, entered = threading.Event(), threading.Event()
    real = VaultStore._merge_into

    def slow(items, fp, secret, origin):
        out = real(items, fp, secret, origin)
        if not entered.is_set():
            entered.set()
            gate.wait(2)
        return out

    with patch.object(VaultStore, "_merge_into", staticmethod(slow)):
        ids = []
        t1 = threading.Thread(target=lambda: ids.append(root.add_item("payment", "c", _CARD).id))
        t1.start(); entered.wait(2)
        t2 = threading.Thread(target=lambda: ids.append(root.add_item("payment", "c", _CARD).id))
        t2.start(); gate.set(); t1.join(5); t2.join(5)
    assert len(set(ids)) == 1 and len(root.list_items()) == 1


def test_legacy_profile_copy_never_shadows_corrected_canonical_card(tmp_path):
    from tools import browser_vault_tool as bvt
    rootdir = tmp_path / "root"
    legacy = VaultStore(rootdir / "profiles" / "w" / "vault", share_payments=False)
    old = legacy.add_item("payment", "c", _CARD, origin="https://old.test")
    root = VaultStore(rootdir / "vault")
    canon = root.add_item("payment", "c", _CARD, origin="https://a.test")
    prof = VaultStore(rootdir / "profiles" / "w" / "vault", share_payments=True)
    saved = prof.add_item("payment", "c", dict(_CARD, cvc="999", billing_postal_code="02139"),
                          origin="https://b.test")
    assert saved.id == canon.id
    pay = [m for m in prof.list_items() if m.kind == "payment"]
    assert [m.id for m in pay] == [canon.id]
    # the legacy copy is preserved (no data loss) but synced, so even its old handle resolves fresh fields
    assert prof.resolve_secret(old.id)["cvc"] == "999"

    class _B:
        name, display_name, needs_unlock = "local", "Hermes", False
        list_items = staticmethod(prof.list_items)
        resolve_secret = staticmethod(prof.resolve_secret)

    with patch("agent.vault_backends.enabled_backends", return_value=[_B()]):
        out = json.loads(bvt.browser_vault_list())
    handles = [i["handle"] for i in out["items"] if i["kind"] == "payment"]
    assert handles == [canon.id]
    assert prof.resolve_secret(handles[0])["billing_postal_code"] == "02139"
    assert "4242424242424242" not in json.dumps(out) and "999" not in json.dumps(out)
