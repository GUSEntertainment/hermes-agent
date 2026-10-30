"""Recognize installed GUS workers from the registry and their native task.

This does not enroll a human, grant account access, or broaden profile tools.
The native worker contract remains responsible for capabilities and approvals.
"""
import os
import re
import sqlite3
import stat
from pathlib import Path

REGISTRY = Path('/Users/Chris/.openclaw/workspace/Agent-Shared/roles/role-registry.yaml')
ROOT = Path('/Users/Chris/.hermes')
# t_9ac68588 (Chris 2026-10-21): no global do-not-run list. Board ownership is
# the privacy wall: a worker is admitted on a board because that board's owner
# enrolled it (Chris's canonical roster here for GUS/Chris boards; the member
# enrollment path in member_worker_policy for member boards).


def protected(path, anchor=None):
    path = Path(path)
    anchor = Path(anchor) if anchor is not None else path.parent
    if not path.is_relative_to(anchor) or anchor.resolve(strict=True) != anchor:
        raise PermissionError('Untrusted GUS source root')
    st = anchor.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise PermissionError('Untrusted GUS source directory')
    directory = anchor
    for part in path.parent.relative_to(anchor).parts:
        directory = directory / part
        st = directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise PermissionError('Untrusted GUS source directory')
    st = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_nlink != 1):
        raise PermissionError('Untrusted GUS worker source')
    return st


def enrolled_profiles(registry=REGISTRY, anchor=None):
    import yaml
    before = protected(registry, anchor)
    data = yaml.safe_load(registry.read_text())
    after = protected(registry, anchor)
    if (before.st_ino, before.st_mtime_ns, before.st_size) != (after.st_ino, after.st_mtime_ns, after.st_size):
        raise PermissionError('GUS registry changed')
    roster = data.get('profile_roster', {})
    canonical = roster.get('canonical', [])
    names = []
    for kind in ('department_owners', 'department_makers', 'review_and_bounded_support'):
        values = roster.get(kind, [])
        if not isinstance(values, list):
            raise PermissionError('Invalid GUS worker registry')
        names.extend(values)
    if not isinstance(canonical, list) or any(not isinstance(n, str) for n in names + canonical):
        raise PermissionError('Invalid GUS worker registry')
    return (set(names) & set(canonical)) | member_owner_workers()


# t_65dff1fd (Chris 2026-10-22): an enrolled member owner's own profile and its
# private owner agents are workers on that owner's board only. Board ownership
# (member_worker_policy.select) stays the privacy wall; this only installs them.
# t_10f94d4e: members come from gateway.member_registry, not a hardcoded name.
class _Boards(dict):
    def _f(self):
        from gateway.member_registry import boards
        return boards()
    def __contains__(self, k): return k in self._f()
    def __getitem__(self, k): return self._f()[k]
    def get(self, k, d=None): return self._f().get(k, d)
    def items(self): return self._f().items()
    def keys(self): return self._f().keys()
    def __iter__(self): return iter(self._f())
MEMBER_OWNER_BOARDS = _Boards()


def owner_agent_binding(profile, root=ROOT):
    """(owner, board) for an installed member owner/agent profile, else None."""
    import yaml
    if not isinstance(profile, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,99}', profile):
        return None
    root = Path(root)
    path = root / 'profiles' / profile / 'config.yaml'
    try:
        protected(path, root)
        config = yaml.safe_load(path.read_text()) or {}
    except (OSError, PermissionError, ValueError):
        return None
    if profile in MEMBER_OWNER_BOARDS:
        row = config.get('app_business_authority') or {}
        if row.get('principal') == profile and row.get('board') == MEMBER_OWNER_BOARDS[profile] and row.get('enabled') is True:
            return profile, row['board']
        return None
    binding = config.get('owner_agent')
    if not isinstance(binding, dict):
        return None
    owner, board = binding.get('owner'), binding.get('board')
    from gateway.member_registry import agent_prefixes
    if owner in MEMBER_OWNER_BOARDS and MEMBER_OWNER_BOARDS[owner] == board and profile.startswith(agent_prefixes()):
        return owner, board
    return None


def member_owner_workers(root=ROOT):
    root = Path(root)
    from gateway.member_registry import agent_prefixes
    out = set()
    try:
        candidates = [p.name for p in (root / 'profiles').iterdir()]
    except OSError:
        return out
    for name in candidates:
        if name in MEMBER_OWNER_BOARDS or name.startswith(agent_prefixes()):
            if owner_agent_binding(name, root):
                out.add(name)
    return out


def current(*, home=None, env=None, registry=REGISTRY, root=ROOT):
    """Return the board for a proven native GUS worker, otherwise no exemption.

    Environment values are locators, not authority: trusted registry membership,
    installed profile and the actual task assignment must all agree.
    """
    from hermes_constants import get_hermes_home
    env = os.environ if env is None else env
    home = Path(get_hermes_home() if home is None else home)
    root = Path(root)
    profile = home.name
    if (home.parent != root / 'profiles'
            or env.get('HERMES_SESSION_SOURCE') != 'kanban'):
        return None
    if profile not in enrolled_profiles(registry, root.parent):
        return None
    if env.get('HERMES_PROFILE') != profile or home.resolve(strict=True) != home:
        raise PermissionError('GUS worker profile mismatch')
    protected(home / 'config.yaml', root)
    if env.get('HERMES_KANBAN_HOME', '').strip():
        raise PermissionError('GUS worker database override denied')
    board = env.get('HERMES_KANBAN_BOARD', '')
    task_id = env.get('HERMES_KANBAN_TASK', '')
    member = owner_agent_binding(profile, root)
    if member is not None and member[1] != board:
        raise PermissionError('Member worker outside its owner board')
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', board) or not re.fullmatch(r't_[A-Za-z0-9_]+', task_id):
        raise PermissionError('Native GUS assignment required')
    db = root / 'kanban.db' if board == 'default' else root / 'kanban' / 'boards' / board / 'kanban.db'
    if env.get('HERMES_KANBAN_DB') != str(db):
        raise PermissionError('GUS worker board mismatch')
    before = protected(db, root)
    for suffix in ('-wal', '-shm', '-journal'):
        side = Path(str(db) + suffix)
        if side.exists() or side.is_symlink():
            protected(side, root)
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
        row = conn.execute('SELECT assignee,runtime_profile FROM tasks WHERE id=?', (task_id,)).fetchone()
    after = protected(db, root)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_uid, s.st_mode, s.st_nlink)
    if identity(before) != identity(after):
        raise PermissionError('Native assignment database changed')
    if not row or row[0] != profile or row[1] not in (None, profile):
        raise PermissionError('GUS worker does not own native assignment')
    return {'profile': profile, 'board': board, 'task': task_id}
