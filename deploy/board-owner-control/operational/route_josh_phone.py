#!/usr/bin/env python3
"""Move the existing authenticated Josh phone route to his native owner profile.

Dry run by default. No caller enrollment, credential, or destination is changed.
Run with --apply after the member-aware main gateway is active. --rollback
reverses only this exact three-field route change.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

CONFIG = Path('/Users/Chris/.hermes/private/phone-assistant/native-conversations.json')
OLD = {'profile': 'app-gus-joshgarcia-margot', 'kind': 'company',
       'endpoint': 'http://127.0.0.1:56640/v1/runs'}
NEW = {'profile': 'joshgarcia', 'kind': 'owner',
       'endpoint': 'http://127.0.0.1:8642/p/joshgarcia/v1/runs'}


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 65536):
            raise PermissionError('Expected current-user private route configuration')
        data = os.read(fd, 65537)
        if os.fstat(fd).st_mtime_ns != info.st_mtime_ns:
            raise RuntimeError('Route configuration changed during read')
        return data
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    before = read_private(CONFIG)
    value = json.loads(before)
    matching = [r for r in value.get('routes', {}).values()
                if r.get('owner') == 'joshgarcia']
    if len(matching) != 1 or matching[0].get('enabled') is not True:
        raise RuntimeError('Expected exactly one enabled Josh phone route')
    route = matching[0]
    expected, target = (NEW, OLD) if args.rollback else (OLD, NEW)
    current = {key: route.get(key) for key in expected}
    if current == target:
        print(json.dumps({'status': 'already_configured', **target}))
        return
    if current != expected:
        raise RuntimeError('Josh route no longer matches the reviewed source configuration')
    route.update(target)
    after = (json.dumps(value, indent=2) + '\n').encode()
    result = {'status': 'ready', 'config_sha256': hashlib.sha256(before).hexdigest(),
              'owner': 'joshgarcia', **target}
    if args.apply:
        backup = CONFIG.with_name(CONFIG.name + '.before-josh-native-' + result['config_sha256'][:12])
        try:
            backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if read_private(backup) != before:
                raise RuntimeError('Protected backup already exists with different contents')
        else:
            with os.fdopen(backup_fd, 'wb') as handle:
                handle.write(before)
                handle.flush()
                os.fsync(handle.fileno())
        fd, temporary = tempfile.mkstemp(prefix='.native-route-', dir=CONFIG.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(after)
                handle.flush()
                os.fsync(handle.fileno())
            if read_private(CONFIG) != before:
                raise RuntimeError('Route configuration changed; refusing to overwrite it')
            os.replace(temporary, CONFIG)
            if read_private(CONFIG) != after:
                raise RuntimeError('Updated route did not match the prepared configuration')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        result['status'] = 'applied'
        result['protected_backup'] = str(backup)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
