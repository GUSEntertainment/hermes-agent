#!/usr/bin/env python3
"""Register the namespace shell before it execs the per-file Python process."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path


def main() -> int:
    domain = Path(os.environ["HERMES_TEST_PROCESS_DOMAIN"]).resolve(strict=True)
    isolation = Path(os.environ["HERMES_TEST_ISOLATION"]).resolve(strict=True)
    token = os.environ["HERMES_TEST_PROCESS_DOMAIN_TOKEN"]
    candidate = domain.stat()
    if (
        domain.parent != isolation
        or not stat.S_ISREG(candidate.st_mode)
        or candidate.st_mode & 0o077
        or (hasattr(os, "getuid") and candidate.st_uid != os.getuid())
        or len(token) != 64
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise RuntimeError("invalid canonical process-domain authority")

    parent = os.getppid()
    if os.getpgid(parent) != os.getpgid(0):
        raise RuntimeError("namespace wrapper left the canonical process group")
    fields = Path(f"/proc/{parent}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    start = fields[19]
    payload = f"{parent}:{start}".encode("ascii")
    record = {
        "pid": parent,
        "start": start,
        "mac": hmac.new(token.encode("ascii"), payload, hashlib.sha256).hexdigest(),
    }
    fd = os.open(domain, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (candidate.st_dev, candidate.st_ino):
            raise RuntimeError("canonical process domain changed before registration")
        os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
