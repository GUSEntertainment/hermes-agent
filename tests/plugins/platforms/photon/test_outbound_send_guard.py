"""Contract tests for the fail-closed iMessage outbound intent guard.

These tests are hermetic: they execute only a fake transport or the sidecar
helper directly. No Photon account, service, port, or live message is used.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from plugins.platforms.photon.adapter import PhotonAdapter

ROOT = Path(__file__).resolve().parents[4]
GUARD_SCRIPT = ROOT / "scripts" / "imessage_send_guard.py"
GUARD_MODULE = ROOT / "hermes_cli" / "imessage_send_guard.py"
NODE_GUARD = ROOT / "plugins" / "platforms" / "photon" / "sidecar" / "outbound-send-guard.mjs"
SIDECAR = ROOT / "plugins" / "platforms" / "photon" / "sidecar" / "index.mjs"


def _fake_imsg(tmp_path: Path) -> tuple[Path, Path]:
    calls = tmp_path / "calls.jsonl"
    fake = tmp_path / "fake-imsg.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_CALLS'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, calls


def _run_guard(tmp_path: Path, fake: Path, calls: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "FAKE_CALLS": str(calls),
            "HERMES_HOME": str(tmp_path / "hermes"),
            "IMSG_GUARD_REAL_BINARY": str(fake),
            "IMSG_GUARD_TEST_MODE": "1",
        }
    )
    return subprocess.run(
        [sys.executable, str(GUARD_SCRIPT), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


def test_guard_script_blocks_identical_send_before_second_transport(tmp_path: Path) -> None:
    fake, calls = _fake_imsg(tmp_path)
    args = ("send", "--chat-guid", "any;+;group", "--text", "hello")
    assert _run_guard(tmp_path, fake, calls, *args).returncode == 0
    blocked = _run_guard(tmp_path, fake, calls, *args)
    assert blocked.returncode == 73
    assert "BLOCKED" in blocked.stderr
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def test_guard_script_kill_switch_prevents_transport(tmp_path: Path) -> None:
    fake, calls = _fake_imsg(tmp_path)
    state = tmp_path / "hermes" / "state"
    state.mkdir(parents=True)
    (state / "imessage-outbound.disabled").write_text("disabled\n", encoding="utf-8")

    blocked = _run_guard(
        tmp_path, fake, calls, "send", "--to", "+15551230001", "--text", "hello"
    )

    assert blocked.returncode == 75
    assert "kill switch" in blocked.stderr
    assert not calls.exists()


def test_concurrent_guard_claims_allow_exactly_one_transport(tmp_path: Path) -> None:
    fake, calls = _fake_imsg(tmp_path)
    args = ("send", "--to", "+15551230001", "--text", "one logical message")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _run_guard(tmp_path, fake, calls, *args), range(8)))

    assert sorted(result.returncode for result in results) == [0, 73, 73, 73, 73, 73, 73, 73]
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def _guard_module():
    spec = importlib.util.spec_from_file_location("imessage_send_guard", GUARD_MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_imessage_skill_selects_installed_guard_not_raw_imsg() -> None:
    skill = (ROOT / "skills" / "apple" / "imessage" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "commands: [hermes-imsg]" in skill
    command_lines = [line.strip() for line in skill.splitlines()]
    assert not any(line.startswith("imsg send") for line in command_lines)
    assert any(line.startswith("hermes-imsg send") for line in command_lines)


def test_packaging_installs_guard_console_entrypoint() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'hermes-imsg = "hermes_cli.imessage_send_guard:main"' in pyproject
    nix_package = (ROOT / "nix" / "hermes-agent.nix").read_text(encoding="utf-8")
    assert '"hermes-imsg"' in nix_package


def test_source_wrapper_is_executable_and_delegates_to_packaged_guard() -> None:
    assert os.access(GUARD_SCRIPT, os.X_OK)
    source = GUARD_SCRIPT.read_text(encoding="utf-8")
    assert "from hermes_cli.imessage_send_guard import main" in source


def _write_stale_legacy_intent(hermes_home: Path, kind: str, target: str, payload: str) -> None:
    guard = _guard_module()
    fingerprint = guard.fingerprint_for(kind, target, payload)
    intent = hermes_home / "state" / "imessage-send-guard" / "intents" / f"{fingerprint}.json"
    intent.parent.mkdir(parents=True)
    intent.write_text("{}\n", encoding="utf-8")
    stale = time.time() - guard.TTL_SECONDS - 60
    os.utime(intent, (stale, stale))


def test_concurrent_stale_python_claims_allow_exactly_one_transport(tmp_path: Path) -> None:
    fake, calls = _fake_imsg(tmp_path)
    target = "+155****0001"
    payload = "expired logical message"
    _write_stale_legacy_intent(tmp_path / "hermes", "text", target, payload)
    args = ("send", "--to", target, "--text", payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _run_guard(tmp_path, fake, calls, *args), range(8)))

    assert sorted(result.returncode for result in results) == [0, 73, 73, 73, 73, 73, 73, 73]
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def _claim_result(guard, target: str, payload: str) -> str:
    try:
        guard.claim_intent("text", target, payload)
    except FileExistsError:
        return "blocked"
    return "sent"


def test_stale_python_replacement_never_unlinks_the_shared_claim_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired replacement is a CAS on a new slot, never unlink-and-recreate."""
    guard = _guard_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    target = "+155****0001"
    payload = "staged expired logical message"
    _write_stale_legacy_intent(tmp_path / "hermes", "text", target, payload)
    fingerprint = guard.fingerprint_for("text", target, payload)
    intent = tmp_path / "hermes" / "state" / "imessage-send-guard" / "intents" / f"{fingerprint}.json"
    real_unlink = Path.unlink

    def forbid_shared_unlink(path: Path, *args, **kwargs):
        assert path != intent, "stale replacement must not unlink a shared claim path"
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", forbid_shared_unlink)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _claim_result(guard, target, payload), range(2)))

    assert sorted(results) == ["blocked", "sent"]


def test_concurrent_stale_node_claims_allow_exactly_one_transport(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    target = "+155****0001"
    payload = "expired logical message"
    _write_stale_legacy_intent(hermes_home, "text", target, payload)
    transports = tmp_path / "node-transports"
    code = (
        f"import {{ claimOutboundIntent }} from {json.dumps(NODE_GUARD.as_uri())};"
        "import fs from 'node:fs';"
        f"claimOutboundIntent({{kind:'text',target:{json.dumps(target)},payload:{json.dumps(payload)}}});"
        f"fs.appendFileSync({json.dumps(str(transports))}, 'sent\\n');"
    )
    env = {**os.environ, "HERMES_HOME": str(hermes_home)}

    def run_node(_: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["node", "--input-type=module", "-e", code], env=env, text=True,
            capture_output=True, check=False,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run_node, range(8)))

    assert sum(result.returncode == 0 for result in results) == 1
    assert transports.read_text(encoding="utf-8").splitlines() == ["sent"]


def test_node_and_python_share_recipient_and_logical_message_identity(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    code = (
        f"import {{ claimOutboundIntent }} from {json.dumps(NODE_GUARD.as_uri())};"
        "claimOutboundIntent({kind:'richlink',target:'any;-;+15551230001',"
        "payload:'https://example.test/hello'});"
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    node = subprocess.run(
        ["node", "--input-type=module", "-e", code], env=env, text=True,
        capture_output=True, check=False,
    )
    assert node.returncode == 0, node.stderr

    fake, calls = _fake_imsg(tmp_path)
    blocked = _run_guard(
        tmp_path, fake, calls, "send", "--chat-guid", "iMessage;-;+15551230001",
        "--text", "https://example.test/hello",
    )
    assert blocked.returncode == 73
    assert not calls.exists()


def test_photon_logical_id_and_direct_cli_share_payload_identity(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    target = "+155****0001"
    payload = "same visible message"
    code = (
        f"import {{ claimOutboundIntent }} from {json.dumps(NODE_GUARD.as_uri())};"
        "claimOutboundIntent({kind:'text',target:" + json.dumps(target) + ",payload:"
        + json.dumps(payload) + ",logicalMessageId:'photon-ledger-42'});"
    )
    env = {**os.environ, "HERMES_HOME": str(hermes_home)}
    node = subprocess.run(
        ["node", "--input-type=module", "-e", code], env=env, text=True,
        capture_output=True, check=False,
    )
    assert node.returncode == 0, node.stderr

    fake, calls = _fake_imsg(tmp_path)
    blocked = _run_guard(tmp_path, fake, calls, "send", "--to", target, "--text", payload)
    assert blocked.returncode == 73
    assert not calls.exists()


def test_node_and_python_share_attachment_caption_identity(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    target = "+15551230001"
    attachment = tmp_path / "captioned.txt"
    attachment.write_text("payload", encoding="utf-8")
    code = (
        f"import {{ attachmentPayload, claimOutboundIntent }} from {json.dumps(NODE_GUARD.as_uri())};"
        f"const p={json.dumps(str(attachment))};"
        f"claimOutboundIntent({{kind:'attachment',target:{json.dumps(target)},"
        "payload:attachmentPayload(p,'caption person@example.com'),"
        "logicalMessageId:'photon-attachment-ledger'});"
    )
    env = {**os.environ, "HERMES_HOME": str(hermes_home)}
    node = subprocess.run(
        ["node", "--input-type=module", "-e", code], env=env, text=True,
        capture_output=True, check=False,
    )
    assert node.returncode == 0, node.stderr

    fake, calls = _fake_imsg(tmp_path)
    blocked = _run_guard(
        tmp_path,
        fake,
        calls,
        "send",
        "--to",
        target,
        "--text",
        "caption person@example.com",
        "--file",
        str(attachment),
    )
    assert blocked.returncode == 73
    assert not calls.exists()


def test_attachment_caption_is_part_of_duplicate_identity(tmp_path: Path) -> None:
    attachment = tmp_path / "caption-identity.txt"
    attachment.write_text("payload", encoding="utf-8")
    code = (
        f"import {{ attachmentPayload, claimOutboundIntent }} from {json.dumps(NODE_GUARD.as_uri())};"
        f"const p={json.dumps(str(attachment))};"
        "claimOutboundIntent({kind:'attachment',target:'+15550000001',"
        "payload:attachmentPayload(p,'first caption'),logicalMessageId:'same-ledger'});"
        "claimOutboundIntent({kind:'attachment',target:'+15550000001',"
        "payload:attachmentPayload(p,'second caption'),logicalMessageId:'same-ledger'});"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        env={**os.environ, "HERMES_HOME": str(tmp_path / "hermes")},
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_guard_blocks_unrecognized_send_shape_instead_of_bypassing(tmp_path: Path) -> None:
    fake, calls = _fake_imsg(tmp_path)
    blocked = _run_guard(tmp_path, fake, calls, "send", "--to", "+155****0001")
    assert blocked.returncode == 76
    assert "payload" in blocked.stderr
    assert not calls.exists()


def test_node_guard_blocks_duplicate_and_honors_kill_switch(tmp_path: Path) -> None:
    code = (
        f"import {{ claimOutboundIntent }} from {json.dumps(NODE_GUARD.as_uri())};"
        "claimOutboundIntent({kind:'text',target:'any;-;+15551230001',payload:'hello'});"
        "try { claimOutboundIntent({kind:'text',target:'+15551230001',payload:'hello'}); }"
        "catch (e) { console.log(e.code); process.exit(0); } process.exit(2);"
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "hermes")
    duplicate = subprocess.run(
        ["node", "--input-type=module", "-e", code], env=env, text=True,
        capture_output=True, check=False,
    )
    assert duplicate.returncode == 0, duplicate.stderr
    assert duplicate.stdout.strip() == "OUTBOUND_INTENT_BLOCKED"

    state = tmp_path / "disabled" / "state"
    state.mkdir(parents=True)
    (state / "imessage-outbound.disabled").write_text("disabled\n", encoding="utf-8")
    code = (
        f"import {{ claimOutboundIntent }} from {json.dumps(NODE_GUARD.as_uri())};"
        "try { claimOutboundIntent({kind:'text',target:'any;-;+15550002',payload:'hello'}); }"
        "catch (e) { console.log(e.code); process.exit(0); } process.exit(2);"
    )
    disabled_env = os.environ.copy()
    disabled_env["HERMES_HOME"] = str(tmp_path / "disabled")
    disabled = subprocess.run(
        ["node", "--input-type=module", "-e", code], env=disabled_env, text=True,
        capture_output=True, check=False,
    )
    assert disabled.returncode == 0, disabled.stderr
    assert disabled.stdout.strip() == "OUTBOUND_INTENT_BLOCKED"


def test_logical_id_does_not_collapse_distinct_poll_and_text_payloads(tmp_path: Path) -> None:
    code = (
        f"import {{ claimOutboundIntent }} from {json.dumps(NODE_GUARD.as_uri())};"
        "claimOutboundIntent({kind:'poll',target:'+155****0001',payload:'poll-body',logicalMessageId:'clar-1'});"
        "claimOutboundIntent({kind:'text',target:'+15550000001',payload:'numbered-body',logicalMessageId:'clar-1'});"
    )
    env = {**os.environ, "HERMES_HOME": str(tmp_path / "hermes")}
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code], env=env, text=True,
        capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr



def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fake_sidecar(tmp_path: Path) -> tuple[Path, Path]:
    sidecar = tmp_path / "sidecar"
    sidecar.mkdir()
    for source in SIDECAR.parent.glob("*.mjs"):
        shutil.copyfile(source, sidecar / source.name)
    package = sidecar / "node_modules" / "spectrum-ts"
    (package / "providers").mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({
        "name": "spectrum-ts", "type": "module",
        "exports": {".": "./index.js", "./providers/imessage": "./providers/imessage.js"},
    }), encoding="utf-8")
    (package / "index.js").write_text(textwrap.dedent("""
        export async function Spectrum() {
          return {
            messages: { [Symbol.asyncIterator]() { return { next: () => new Promise(() => {}) }; } },
            stop: async () => undefined,
          };
        }
        const builder = (kind) => (...args) => ({kind, args});
        export const attachment = builder('attachment');
        export const voice = builder('voice');
        export const text = builder('text');
        export const markdown = builder('markdown');
        export const richlink = builder('richlink');
        export const typing = builder('typing');
        export const poll = builder('poll');
    """).lstrip(), encoding="utf-8")
    transports = tmp_path / "sidecar-transports.jsonl"
    (package / "providers" / "imessage.js").write_text(textwrap.dedent("""
        import fs from 'node:fs';
        const space = {
          id: 'fake-space',
          async send(value) {
            fs.appendFileSync(process.env.FAKE_TRANSPORTS, JSON.stringify(value) + '\\n');
            if (value.kind === 'text' && process.env.FAKE_CAPTION_OUTCOME === 'rejected') {
              throw new Error('forbidden caption');
            }
            if (value.kind === 'text' && process.env.FAKE_CAPTION_OUTCOME === 'timeout') {
              throw new Error('caption timeout after unknown outcome');
            }
            return {id: 'fake-message'};
          },
        };
        export function imessage() {
          return {space: {create: async () => space, get: async () => space}};
        }
        imessage.config = () => ({});
        imessage.effect = {message: {confetti: 'effect-confetti'}};
        export const effect = (value, id) => ({kind: 'effect', value, id});
    """).lstrip(), encoding="utf-8")
    return sidecar, transports


def _post(port: int, endpoint: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{endpoint}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Hermes-Sidecar-Token": "test-token"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_sidecar_http_endpoints_claim_before_fake_transport(tmp_path: Path) -> None:
    sidecar, transports = _fake_sidecar(tmp_path)
    attachment = tmp_path / "attachment.txt"
    attachment.write_text("payload", encoding="utf-8")
    cases = [
        (
            "/send",
            {"spaceId": "+15550000001", "text": "hello", "logicalMessageId": "ledger-text"},
            {"spaceId": "+15550000001", "text": "hello", "logicalMessageId": "different-ledger-text"},
        ),
        (
            "/send-richlink",
            {"spaceId": "+15550000001", "url": "https://example.test", "logicalMessageId": "ledger-link"},
            {"spaceId": "+15550000001", "url": "https://example.test", "logicalMessageId": "different-ledger-link"},
        ),
        (
            "/send-attachment",
            {"spaceId": "+15550000001", "path": str(attachment), "kind": "attachment", "logicalMessageId": "ledger-attachment"},
            {"spaceId": "+15550000001", "path": str(attachment), "kind": "attachment", "logicalMessageId": "different-ledger-attachment"},
        ),
        (
            "/send-poll",
            {"spaceId": "+15550000001", "title": "Pick", "options": ["A", "B"], "logicalMessageId": "ledger-poll"},
            {"spaceId": "+15550000001", "title": "Pick", "options": ["A", "B"], "logicalMessageId": "different-ledger-poll"},
        ),
        (
            "/send-attachment",
            {"spaceId": "+15550000001", "path": str(attachment), "kind": "voice", "logicalMessageId": "ledger-voice"},
            {"spaceId": "+15550000001", "path": str(attachment), "kind": "voice", "logicalMessageId": "different-ledger-voice"},
        ),
        (
            "/send-effect",
            {"spaceId": "+15550000001", "text": "hooray", "effect": "confetti"},
            {"spaceId": "+15550000001", "text": "hooray", "effect": "confetti"},
        ),
    ]
    port = _free_port()
    env = {
        **os.environ, "HERMES_HOME": str(tmp_path / "hermes"),
        "PHOTON_PROJECT_ID": "test-project", "PHOTON_PROJECT_SECRET": "test-secret",
        "PHOTON_SIDECAR_PORT": str(port), "PHOTON_SIDECAR_TOKEN": "test-token",
        "FAKE_TRANSPORTS": str(transports),
    }
    proc = subprocess.Popen(
        ["node", "index.mjs"], cwd=sidecar, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                status, _ = _post(port, "/healthz", {})
                assert status == 200
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

        for endpoint, initial, recovered in cases:
            assert _post(port, endpoint, initial)[0] == 200
            assert _post(port, endpoint, recovered)[0] == 409

        logical_id = "clarify-http-identity"
        assert _post(port, "/send-poll", {
            "spaceId": "+155****0002", "title": "Choose", "options": ["A", "B"],
            "logicalMessageId": logical_id,
        })[0] == 200
        assert _post(port, "/send", {
            "spaceId": "+155****0002", "text": "1. A\n2. B",
            "logicalMessageId": logical_id,
        })[0] == 200
    finally:
        proc.terminate()
        proc.communicate(timeout=5)

    assert len(transports.read_text(encoding="utf-8").splitlines()) == len(cases) + 2


@pytest.mark.parametrize(
    ("outcome", "delivery_state", "error_class"),
    [
        ("rejected", "failed", "composite_caption_rejected"),
        ("timeout", "ambiguous", "composite_caption_ambiguous"),
    ],
)
def test_caption_failure_is_explicit_and_composite_is_never_automatically_retried(
    tmp_path: Path, outcome: str, delivery_state: str, error_class: str,
) -> None:
    sidecar, transports = _fake_sidecar(tmp_path)
    attachment = tmp_path / "captioned.txt"
    attachment.write_text("payload", encoding="utf-8")
    port = _free_port()
    env = {
        **os.environ, "HERMES_HOME": str(tmp_path / "hermes"),
        "PHOTON_PROJECT_ID": "test-project", "PHOTON_PROJECT_SECRET": "test-secret",
        "PHOTON_SIDECAR_PORT": str(port), "PHOTON_SIDECAR_TOKEN": "test-token",
        "FAKE_TRANSPORTS": str(transports), "FAKE_CAPTION_OUTCOME": outcome,
    }
    proc = subprocess.Popen(
        ["node", "index.mjs"], cwd=sidecar, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    body = {
        "spaceId": "+155****0001", "path": str(attachment),
        "kind": "attachment", "caption": "visible caption",
        "logicalMessageId": "composite-one-attempt",
    }
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                assert _post(port, "/healthz", {})[0] == 200
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

        status, response = _post(port, "/send-attachment", body)
        assert status == 502
        assert response == {
            "ok": False,
            "error": "attachment sent but caption delivery did not complete",
            "error_class": error_class,
            "retryable": False,
            "delivery_state": delivery_state,
            "attachment_sent": True,
            "automatic_retry_safe": False,
        }
        assert len(transports.read_text(encoding="utf-8").splitlines()) == 2

        assert _post(port, "/send-attachment", body)[0] == 409
        assert len(transports.read_text(encoding="utf-8").splitlines()) == 2
    finally:
        proc.terminate()
        proc.communicate(timeout=5)


def test_media_without_caption_completes_with_one_transport_call(tmp_path: Path) -> None:
    sidecar, transports = _fake_sidecar(tmp_path)
    attachment = tmp_path / "plain.txt"
    attachment.write_text("payload", encoding="utf-8")
    port = _free_port()
    env = {
        **os.environ, "HERMES_HOME": str(tmp_path / "hermes"),
        "PHOTON_PROJECT_ID": "test-project", "PHOTON_PROJECT_SECRET": "test-secret",
        "PHOTON_SIDECAR_PORT": str(port), "PHOTON_SIDECAR_TOKEN": "test-token",
        "FAKE_TRANSPORTS": str(transports), "FAKE_CAPTION_OUTCOME": "rejected",
    }
    proc = subprocess.Popen(
        ["node", "index.mjs"], cwd=sidecar, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                assert _post(port, "/healthz", {})[0] == 200
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        status, response = _post(port, "/send-attachment", {
            "spaceId": "+155****0001", "path": str(attachment), "kind": "attachment",
        })
        assert status == 200
        assert response["ok"] is True
        assert len(transports.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        proc.terminate()
        proc.communicate(timeout=5)


@pytest.mark.asyncio
async def test_photon_send_retry_hook_is_strictly_single_attempt() -> None:
    adapter = object.__new__(PhotonAdapter)
    failure = SendResult(success=False, error="ConnectError", retryable=True)
    adapter.send = AsyncMock(return_value=failure)

    result = await PhotonAdapter._send_with_retry(
        adapter, "any;+;group", "hello", max_retries=99, base_delay=0,
    )

    assert result is failure
    adapter.send.assert_awaited_once_with(
        chat_id="any;+;group", content="hello", reply_to=None, metadata=None,
    )
