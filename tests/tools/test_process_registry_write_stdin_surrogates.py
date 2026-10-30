"""Sibling regression test for #79178: background-PTY stdin must round-trip
surrogateescape content instead of crashing on the strict UTF-8 encode."""

import shlex
import time

import pytest

from tools.process_registry import ProcessRegistry


@pytest.mark.platforms("linux")
def test_write_stdin_pty_surrogateescape_roundtrip(tmp_path):
    registry = ProcessRegistry()
    out = tmp_path / "out.bin"
    script = tmp_path / "read_stdin.py"
    # readline(): a PTY never delivers EOF, so read one line (canonical mode
    # delivers it after the newline we send).
    # Open first, then pause before writing to deterministically expose the
    # existence-before-write window that broad parallel runs used to hit.
    script.write_text(
        "import sys\n"
        "import time\n"
        f"with open({str(out)!r}, 'wb') as output:\n"
        "    time.sleep(0.25)\n"
        "    output.write(sys.stdin.buffer.readline())\n"
    )
    session = registry.spawn_local(
        f"python3 {shlex.quote(str(script))}",
        cwd=str(tmp_path),
        use_pty=True,
    )
    if session._pty is None:
        registry.kill_process(session.id)
        pytest.skip("ptyprocess not available; PTY path not exercised")
    try:
        result = registry.write_stdin(
            session.id, b"\xff".decode("utf-8", "surrogateescape") + "\n"
        )
        assert result["status"] == "ok", result
        expected = b"\xff\n"
        deadline = time.monotonic() + 30
        observed = None
        terminal = registry.poll(session.id)
        while time.monotonic() < deadline:
            observed = out.read_bytes() if out.exists() else None
            terminal = registry.poll(session.id)
            if observed == expected or terminal["status"] == "exited":
                break
            time.sleep(0.05)
        assert observed == expected, (
            f"PTY output did not reach {expected!r}; observed={observed!r}; "
            f"terminal={terminal!r}"
        )
    finally:
        registry.kill_process(session.id)
