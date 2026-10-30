"""Chris (admin) may file to chris + default boards; members stay on their own."""
import os, tempfile
from pathlib import Path
import pytest
from gateway import app_business_authority as aba

def _root(tmp, profile, principal, board, allowed):
    root = Path(tmp).resolve(); os.chmod(root, 0o700)
    home = root / 'profiles' / profile; home.mkdir(parents=True)
    os.chmod(root / 'profiles', 0o700); os.chmod(home, 0o700)
    lines = ''.join(f'  - {b}\n' for b in allowed)
    (home / 'config.yaml').write_text(
        f"toolsets: [hermes-cli]\napp_business_authority:\n  principal: {principal}\n"
        f"  board: {board}\n  allowed_boards:\n{lines}  enabled: true\n"
        "  scope_contract: gus-company-work-v1\n")
    os.chmod(home / 'config.yaml', 0o600)
    return root

def _grant(profile, principal, board, allowed):
    with tempfile.TemporaryDirectory() as tmp:
        root = _root(tmp, profile, principal, board, allowed)
        enr = lambda u: {'owner': u, 'profile': u, 'board': board}
        return aba.current(profile, principal, root=root, enrollment=enr)['allowed_boards']

def test_live_chris_app_config_grants_chris_and_default():
    import yaml
    row = yaml.safe_load(open('/Users/Chris/.hermes/profiles/app-gus-chris-margot/config.yaml'))['app_business_authority']
    assert row['principal'] == 'chris'
    assert {'chris', 'default', 'chris-home'} <= set(row['allowed_boards'])
    b = _grant('app-gus-chris-margot', 'chris', 'chris-home', row['allowed_boards'])
    assert 'chris' in b and 'default' in b

@pytest.mark.parametrize('cfg', ['app-gus-connor-margot', 'app-gus-joshgarcia-margot'])
def test_live_member_configs_stay_on_own_board(cfg):
    import yaml
    row = yaml.safe_load(open(f'/Users/Chris/.hermes/profiles/{cfg}/config.yaml'))['app_business_authority']
    assert row['allowed_boards'] == [row['board']]
    for other in ('chris', 'chris-home', 'default', 'connor-home', 'josh-home'):
        if other != row['board']:
            assert other not in row['allowed_boards']

def test_member_to_other_member_board_denied_by_return_capture():
    # Mirrors task_return_intents.capture board gate.
    b = _grant('app-gus-joshgarcia-margot', 'joshgarcia', 'josh-home', ['josh-home'])
    for board in ('connor-home', 'chris', 'chris-home'):
        assert board not in b
