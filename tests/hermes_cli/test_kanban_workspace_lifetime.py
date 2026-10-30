"""Real lifecycle I/O regressions for active handoff input lifetime."""
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    c = kb.connect(home / 'kanban/kanban.db')
    yield c
    c.close()


def task_workspace(c, title='parent'):
    tid = kb.create_task(c, title=title, assignee='maker')
    workspace = kb.resolve_workspace(kb.get_task(c, tid))
    kb.set_workspace_path(c, tid, workspace)
    return tid, workspace


def body_for(path):
    return '```worker-requirements\n' + json.dumps({'read_paths': [str(path)]}) + '\n```'


def chain(c, *, reference=True):
    parent, workspace = task_workspace(c)
    source = workspace / 'input.txt'
    source.write_text('verified source bytes')
    child = kb.create_task(c, title='intermediate', workspace_kind='dir', workspace_path=str(workspace), parents=[parent])
    grandchild, own = task_workspace(c, 'grandchild')
    kb.link_tasks(c, child, grandchild)
    if reference:
        with kb.write_txn(c):
            c.execute('UPDATE tasks SET body=? WHERE id=?', (body_for(source), grandchild))
    assert kb.complete_task(c, parent)
    return parent, child, grandchild, workspace, own, source


def test_grandchild_reference_survives_dir_child_completion_then_releases(board):
    parent, child, grandchild, workspace, own, source = chain(board)
    assert kb.complete_task(board, child)
    assert source.read_text() == 'verified source bytes'
    assert kb.complete_task(board, grandchild)
    assert not workspace.exists(), 'terminal grandchild rechecks all ancestors'


def test_verified_task_owned_snapshot_releases_parent_before_grandchild_done(board):
    parent, child, grandchild, workspace, own, source = chain(board)
    assert kb.complete_task(board, child)
    snapshot = own / 'immutable-input.txt'
    snapshot.write_bytes(source.read_bytes())
    assert hashlib.sha256(snapshot.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET body=? WHERE id=?', (body_for(snapshot), grandchild))
    kb._try_cleanup_parent_workspaces(board, grandchild)
    assert not workspace.exists()
    assert snapshot.read_text() == 'verified source bytes'
    assert kb.get_task(board, grandchild).status not in ('done', 'archived')


def test_nonreferencing_grandchild_does_not_retain_entire_ancestor_tree(board):
    parent, child, grandchild, workspace, own, source = chain(board, reference=False)
    assert kb.complete_task(board, child)
    assert not workspace.exists()
    assert own.exists()


@pytest.mark.parametrize('reference_kind', ['direct', 'relative', 'normalized', 'symlink', 'symlink_loop', 'legacy_prose', 'malformed'])
def test_active_nonchild_exact_reference_is_retained(board, reference_kind):
    parent, workspace = task_workspace(board)
    source = workspace / 'input.txt'
    source.write_text('keep')
    holder, own = task_workspace(board, 'input owner')
    if reference_kind == 'relative':
        path = Path('..') / workspace.name / 'input.txt'
    elif reference_kind == 'normalized':
        (workspace / 'sub').mkdir()
        path = workspace / 'sub/../input.txt'
    elif reference_kind == 'symlink_loop':
        path = workspace / 'loop'
        path.symlink_to('loop')
    elif reference_kind == 'symlink':
        path = own / 'input-link'
        path.symlink_to(source)
    else:
        path = source
    body = body_for(path)
    if reference_kind == 'legacy_prose':
        body = f'Inspect source input at {source} before diagnosis.'
    if reference_kind == 'malformed':
        body = '```worker-requirements\nnot-json\n```\nRequired input ' + str(source)
    with kb.write_txn(board):
        board.execute('UPDATE tasks SET body=? WHERE id=?', (body, holder))
    assert kb.complete_task(board, parent)
    assert source.read_text() == 'keep'
    assert kb.complete_task(board, holder)
    kb._cleanup_workspace(board, parent)
    assert not workspace.exists()


def test_similar_prefix_and_broad_home_workspace_do_not_retain_unrelated_scratch(board, tmp_path):
    parent, workspace = task_workspace(board)
    kb.create_task(board, title='broad workspace', workspace_kind='dir', workspace_path=str(tmp_path))
    kb.create_task(board, title='different exact input', body=body_for(str(workspace) + '-other/file'))
    assert kb.complete_task(board, parent)
    assert not workspace.exists()


def test_running_parent_is_never_deleted_by_a_terminal_context_child(board):
    parent, workspace = task_workspace(board)
    child = kb.create_task(board, title='completed child', parents=[parent])
    assert kb.claim_task(board, parent)
    # Cleanup can be called on legacy/recovered board states independently of
    # normal transition predicates; a running ancestor is never disposable.
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
    kb._try_cleanup_parent_workspaces(board, child)
    assert workspace.exists()
    kb._cleanup_workspace(board, parent)
    assert workspace.exists()


def test_dir_alias_completion_artifact_is_preserved_before_ancestor_cleanup(board):
    parent, workspace = task_workspace(board)
    child = kb.create_task(board, title='shared dir child', workspace_kind='dir', workspace_path=str(workspace), parents=[parent])
    artifact = workspace / 'review.txt'
    artifact.write_text('accepted exact artifact')
    assert kb.complete_task(board, parent)
    metadata = {'artifacts': [str(artifact)]}
    assert kb.complete_task(board, child, summary='verified', metadata=metadata)
    assert not workspace.exists()
    attachments = kb.list_attachments(board, child)
    assert len(attachments) == 1
    assert Path(attachments[0].stored_path).read_text() == 'accepted exact artifact'


def test_active_shared_workspace_is_retained_without_a_dependency(board):
    parent, workspace = task_workspace(board)
    kb.create_task(board, title='shared consumer', workspace_kind='dir', workspace_path=str(workspace))
    assert kb.complete_task(board, parent)
    assert workspace.exists()


@pytest.mark.parametrize('kind', ['scratch', 'dir', 'none'])
def test_worker_metadata_cannot_forge_preserved_attachment(board, tmp_path, kind):
    tid, workspace = task_workspace(board)
    if kind != 'scratch':
        with kb.write_txn(board):
            board.execute('UPDATE tasks SET workspace_kind=?,workspace_path=? WHERE id=?',
                          (kind, str(workspace) if kind=='dir' else None, tid))
    outsider=tmp_path/'outside.txt';outsider.write_text('private outside source')
    assert kb.complete_task(board,tid,summary='finished',metadata={'_staged_artifacts':[str(outsider)]})
    assert kb.list_attachments(board,tid)==[]
    assert outsider.read_text()=='private outside source'
    assert '_staged_artifacts' not in (kb.latest_run(board,tid).metadata or {})


def test_real_preservation_cannot_include_forged_internal_or_prefix_paths(board, tmp_path):
    tid,workspace=task_workspace(board)
    source=workspace/'output.txt';source.write_text('authorized output')
    attachment_root=kb.task_attachments_dir(tid)
    outsider=Path(str(attachment_root)+'-outside')/'private.txt'
    outsider.parent.mkdir(parents=True);outsider.write_text('outside data')
    assert kb.complete_task(board,tid,summary='finished',metadata={
        'artifacts':[str(source),str(outsider)],'_staged_artifacts':[str(outsider)],
    })
    attachments=kb.list_attachments(board,tid)
    assert len(attachments)==1
    assert Path(attachments[0].stored_path).read_text()=='authorized output'
    assert Path(attachments[0].stored_path).resolve().is_relative_to(attachment_root.resolve())
    assert outsider.read_text()=='outside data'
