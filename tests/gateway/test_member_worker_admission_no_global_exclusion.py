"""t_9ac68588 regression: no global do-not-run list may re-exclude member workers.

Board ownership is the privacy wall. A member profile is admitted on its own
owner's board and refused on another owner's board; no hardcoded profile list
participates in admission.
"""
import ast
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOME = Path('/Users/Chris/.hermes')


def test_worker_authority_has_no_global_exclusion_list():
    import gateway.gus_worker_authority as gwa
    assert not hasattr(gwa, 'EXCLUDED'), 'global EXCLUDED gate returned'
    import hermes_cli.blocked_lane_recovery as blr
    assert not hasattr(blr, 'NON_WORKER'), 'global NON_WORKER gate returned'


@pytest.mark.parametrize('rel', [
    'gateway/gus_worker_authority.py', 'hermes_cli/blocked_lane_recovery.py',
    'gateway/member_worker_policy.py', 'hermes_cli/kanban_db.py',
])
def test_no_hardcoded_member_profile_set_in_admission_sources(rel):
    tree = ast.parse((ROOT / rel).read_text())
    members = {'joshgarcia', 'family-eliana', 'britt'}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Set, ast.Call)) and isinstance(getattr(node, 'func', None), ast.Name) \
                and node.func.id == 'frozenset' or isinstance(node, ast.Set):
            consts = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            assert not (consts & members and {'chris', 'default'} & consts), f'{rel}: profile deny-list {consts}'


def test_review_digest_compare_accepts_sha256_prefix(tmp_path):
    from hermes_cli import owner_cell as oc
    f = tmp_path / 'ready.txt'
    f.write_bytes(b'READY\n')
    bare = oc._regular_file_sha256(f)
    assert bare == oc.normalize_digest('sha256:' + bare)
    src = (ROOT / 'hermes_cli/kanban_db.py').read_text()
    assert "!=goal['review_artifact_digest']" not in src


live = pytest.mark.skipif(not (HOME / 'kanban/boards/josh-home/kanban.db').is_file(), reason='live boards absent')


def _any_task(board):
    import sqlite3
    db = HOME / 'kanban/boards' / board / 'kanban.db'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as c:
        row = c.execute("SELECT id FROM tasks WHERE assignee='joshgarcia' ORDER BY created_at DESC LIMIT 1").fetchone() \
            or c.execute('SELECT id FROM tasks LIMIT 1').fetchone()
    return row[0]


@live
def test_member_profile_admitted_on_own_board():
    from gateway.member_worker_policy import select
    policy = select(_any_task('josh-home'), 'josh-home', 'joshgarcia')
    assert policy and policy['principal'] == 'joshgarcia' and policy['board'] == 'josh-home'


@live
def test_member_profile_refused_on_other_owner_board():
    from gateway.member_worker_policy import select
    if not (HOME / 'kanban/boards/connor-home/kanban.db').is_file():
        pytest.skip('connor-home absent')
    with pytest.raises(PermissionError):
        select(_any_task('connor-home'), 'connor-home', 'joshgarcia')


@live
def test_member_profile_never_in_chris_roster():
    from gateway.gus_worker_authority import enrolled_profiles
    names = enrolled_profiles()
    assert 'joshgarcia' not in names and not any(n.startswith('josh-agent-') for n in names)
