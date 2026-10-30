"""t_82ea5042: exact legacy owner board mapping; others stay denied."""
from pathlib import Path
import pytest
import gateway.member_worker_policy as mwp

HOME=Path('/Users/Chris/.hermes')
live=pytest.mark.skipif(not (HOME/'kanban/boards/chris/kanban.db').is_file(),reason='live boards absent')

def test_map_is_exact():
    assert mwp.LEGACY_OWNER_BOARDS=={'chris':'chris'}

@live
def test_legacy_chris_board_resolves_to_owner():
    assert mwp.select('t_x','chris','scarlett') is None

@live
@pytest.mark.parametrize('board',['chris-legacy','chris2','chrisx','unknown-board-zz'])
def test_unknown_or_alias_boards_denied(board,monkeypatch):
    import gateway.private_action_worker_policy as pa, gateway.owner_delegation as od
    monkeypatch.setattr(pa,'current',lambda *a:None);monkeypatch.setattr(od,'worker_policy',lambda *a:None)
    real=Path.exists
    monkeypatch.setattr(Path,'exists',lambda self: True if self==HOME/'kanban/boards'/board else real(self))
    with pytest.raises(PermissionError,match='Native board owner unavailable'):
        mwp.select('t_x',board,'scarlett')

@live
def test_member_claim_on_legacy_slug_fails_closed(monkeypatch):
    import gateway.member_registry as mr
    monkeypatch.setattr(mr,'boards',lambda:{'joshgarcia':'chris'})
    with pytest.raises(PermissionError):
        mwp.select('t_x','chris','scarlett')

@live
def test_other_owner_board_still_walled():
    with pytest.raises(PermissionError):
        mwp.select('t_x','josh-home','scarlett')
