#!/usr/bin/env python3
"""One-time installation of private member-controller transport material."""
import argparse
import os
from pathlib import Path
import secrets
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.runtime.resolve(strict=True)))
    from gateway import board_control_transport as transport
    from gateway.app_business_authority import protected
    folder = transport.key_path().parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    protected(folder / 'key', transport.ROOT) if (folder / 'key').exists() else None
    if not transport.key_path().exists():
        fd = os.open(transport.key_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, secrets.token_bytes(32))
            os.fsync(fd)
        finally:
            os.close(fd)
    transport._key()
    if not transport.ledger_path().exists():
        transport.initialize_ledger(transport.ledger_path())
    transport._ledger_stat()
    print('Member controller signing key and replay ledger installed; values not displayed.')


if __name__ == '__main__':
    main()
