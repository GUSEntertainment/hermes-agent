"""Contract tests for safe native-voice attachment naming.

The helper is executed under Node directly; no sidecar process or external
messaging service is started.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

VOICE_HELPER = Path("plugins/platforms/photon/sidecar/voice-attachment.mjs").resolve()


def test_voice_attachment_uses_safe_m4a_name_and_keeps_source_mime() -> None:
    harness = (
        f"import {{ buildAttachmentContent }} from {json.dumps(VOICE_HELPER.as_uri())};"
        "const out = buildAttachmentContent("
        "{path:'/tmp/voice.MP3',name:'../../My Voice.MP3',mimeType:'audio/mpeg',kind:'voice'},"
        "{voice:(path, options)=>({type:'voice',path,...options}),attachment:()=>{throw new Error('wrong builder')}});"
        "process.stdout.write(JSON.stringify(out));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "type": "voice",
        "path": "/tmp/voice.MP3",
        "name": "My Voice.m4a",
        "mimeType": "audio/mpeg",
    }


def test_standard_attachment_metadata_is_unchanged() -> None:
    harness = (
        f"import {{ buildAttachmentContent }} from {json.dumps(VOICE_HELPER.as_uri())};"
        "const out = buildAttachmentContent("
        "{path:'/tmp/report.pdf',name:'Q3 report.pdf',mimeType:'application/pdf',kind:'attachment'},"
        "{voice:()=>{throw new Error('wrong builder')},attachment:(path, options)=>({type:'attachment',path,...options})});"
        "process.stdout.write(JSON.stringify(out));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "type": "attachment",
        "path": "/tmp/report.pdf",
        "name": "Q3 report.pdf",
        "mimeType": "application/pdf",
    }
