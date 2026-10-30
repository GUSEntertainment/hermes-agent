"""A shared data home must not lend one install another install's identity."""
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import banner, build_info, __version__


@pytest.fixture
def installs(tmp_path, monkeypatch):
    home = tmp_path / 'data'
    home.mkdir()
    old = home / 'hermes-agent'
    (old / '.git').mkdir(parents=True)
    current = tmp_path / 'release'
    (current / 'hermes_cli').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('HERMES_REVISION', raising=False)
    monkeypatch.setattr(banner, '__file__', str(current / 'hermes_cli/banner.py'))
    monkeypatch.setattr(banner, '_git_banner_state_cache', None)
    monkeypatch.setattr(build_info, '_BUILD_SHA_FILE', current / '.hermes_build_sha')
    monkeypatch.setattr('hermes_cli.config.detect_install_method', lambda root: 'unknown')
    return home, old, current


def test_packaged_release_uses_own_build_sha_with_legacy_checkout_present(installs):
    home, old, current = installs
    sha = 'b' * 40
    (current / '.hermes_build_sha').write_text(sha)
    assert banner._resolve_repo_dir() is None
    assert banner.get_git_banner_state() == {'upstream': sha[:8], 'local': sha[:8], 'ahead': 0}


def test_no_build_identity_does_not_borrow_legacy_git(installs, monkeypatch):
    probe = Mock()
    monkeypatch.setattr(banner, '_git_stdout', probe)  # upstream v0.21.5 folded _git_short_hash into _git_stdout
    assert banner.get_git_banner_state() is None
    probe.assert_not_called()


def test_packaged_release_ignores_legacy_update_cache(installs, monkeypatch):
    home, old, current = installs
    cache = home / '.update_check'
    cache.write_text(json.dumps({'ts': time.time(), 'ver': __version__, 'rev': None, 'behind': 1234}))
    before = cache.read_bytes()
    probe = Mock()
    monkeypatch.setattr(banner, '_check_via_local_git', probe)
    assert banner.check_for_updates() is None
    probe.assert_not_called()
    assert cache.read_bytes() == before


@pytest.mark.parametrize('prior_root', [None, 'other'])
def test_git_install_rejects_unscoped_or_other_install_cache(installs, monkeypatch, prior_root):
    home, old, current = installs
    (current / '.git').mkdir()
    cached = {'ts': time.time(), 'ver': __version__, 'rev': None, 'behind': 1234}
    if prior_root:
        cached['install_root'] = str(old)
    (home / '.update_check').write_text(json.dumps(cached))
    probe = Mock(return_value=2)
    monkeypatch.setattr(banner, '_check_via_local_git', probe)
    assert banner.check_for_updates() == 2
    probe.assert_called_once_with(current.resolve())
    probe.reset_mock()
    assert banner.check_for_updates() == 2
    probe.assert_not_called()


def test_source_checkout_wins_over_baked_identity_and_legacy_checkout(installs):
    home, old, current = installs
    (current / '.git').write_text('gitdir: elsewhere\n')
    (current / '.hermes_build_sha').write_text('b' * 40)
    assert banner._resolve_repo_dir() == current.resolve()


def test_embedded_revision_still_checks_and_caches_without_git(installs, monkeypatch):
    monkeypatch.setenv('HERMES_REVISION', 'revision-a')
    probe = Mock(return_value=3)
    monkeypatch.setattr(banner, '_check_via_rev', probe)
    assert banner.check_for_updates() == 3
    assert banner.check_for_updates() == 3
    probe.assert_called_once_with('revision-a')
    monkeypatch.setenv('HERMES_REVISION', 'revision-b')
    assert banner.check_for_updates() == 3
    assert probe.call_count == 2


@pytest.mark.parametrize('has_git,expected', [(False, 'packaged release'), (True, 'git')])
def test_version_install_label_matches_running_code(installs, monkeypatch, capsys, has_git, expected):
    from hermes_cli import _startup_fast

    home, old, current = installs
    (current / '.hermes_build_sha').write_text('b' * 40)
    if has_git:
        (current / '.git').mkdir()
    monkeypatch.setattr(_startup_fast, 'project_root_str', lambda: str(current))
    monkeypatch.setattr('hermes_cli.config.detect_install_method', lambda root: 'git')
    _startup_fast.print_fast_version_info(check_updates=False)
    assert f'Install method: {expected}' in capsys.readouterr().out
