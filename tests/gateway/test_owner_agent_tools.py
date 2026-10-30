"""Owner-agent capability projection canaries (synthetic Josh-org agent)."""
import json
import pytest

from gateway import member_worker_policy as mwp
from toolsets import resolve_multiple_toolsets

PROFILE_TOOLSETS = ['terminal', 'file', 'kanban', 'worker_skills', 'todo', 'web', 'browser', 'memory',
                    'session_search', 'vision', 'image_gen', 'code_execution', 'delegation']


def cfg(owner='joshgarcia', board='josh-home'):
    return {'toolsets': list(PROFILE_TOOLSETS), 'mcp_servers': {'x': {}},
            'owner_agent': {'owner': owner, 'id': 'oa_' + '0' * 32, 'board': board},
            'memory': {'memory_enabled': True}}


def pol(principal='joshgarcia', board='josh-home', action='owner_work:t_x'):
    return dict(version=mwp.PRIVATE_VERSION, principal=principal, board=board, task='t_x',
                worker='josh-agent-' + '0' * 32, action=action, authority_fingerprint='f')


def test_josh_agent_gets_working_tools():
    out = mwp.effective(cfg(), pol())
    ts = set(out['toolsets'])
    assert {'web', 'vision', 'image_gen', 'browser', 'terminal', 'file', 'worker_skills', 'delegation'} <= ts
    # Privacy wall: no memory, history, shared history, MCP, terminal network.
    assert not ts & {'memory', 'session_search', 'shared_history', 'skills', 'cronjob', 'clarify'}
    assert out['mcp_servers'] == {} and out['memory']['memory_enabled'] is False
    assert out['security']['workspace_confinement']['deny_terminal_network'] is True
    assert out['security']['workspace_confinement']['deny_home_reads_outside_workspace'] is True
    tools = set(resolve_multiple_toolsets(out['toolsets']))
    assert 'send_message' not in tools and 'session_search' not in tools and 'memory' not in tools


@pytest.mark.parametrize('c,p', [
    (cfg(owner='connor'), pol()),                 # agent owned by someone else
    (cfg(board='chris-home'), pol()),             # board mismatch
    (cfg(), pol(principal='connor')),             # principal without grant
    (cfg(), pol(action='app_xyz')),               # not owner-delegated work
    ({'toolsets': ['web']}, pol()),               # no owner_agent binding
])
def test_no_extras_outside_owner_scope(c, p):
    ts = set(mwp.effective(c, p)['toolsets'])
    assert not ts & {'vision', 'image_gen', 'browser'}


def test_company_member_policy_unchanged():
    p = dict(version=mwp.VERSION, principal='joshgarcia', board='josh-home', task='t', worker='w')
    assert not set(mwp.effective(cfg(), p)['toolsets']) & {'vision', 'image_gen', 'browser'}
    assert mwp.effective(cfg(), None) == cfg()


def test_profile_declaration_still_required():
    c = cfg(); c['toolsets'] = [t for t in PROFILE_TOOLSETS if t != 'image_gen']
    assert 'image_gen' not in mwp.effective(c, pol())['toolsets']


def test_allowed_tools_drop_raw_cdp(monkeypatch):
    import agent.private_worker_mode as pwm
    out = mwp.effective(cfg(), pol())
    contract = {'member_company_policy': pol(), 'toolsets': out['toolsets'], 'workspace_confinement': True}
    monkeypatch.setattr(pwm, 'get_authenticated_worker_contract', lambda: contract)
    monkeypatch.setattr(mwp, 'verify', lambda p: None)
    allowed = mwp.allowed_tools()
    assert {'browser_navigate', 'vision_analyze', 'image_generate', 'web_extract'} <= allowed
    assert not allowed & {'browser_cdp', 'browser_exec', 'send_message', 'session_search', 'memory'}


@pytest.mark.parametrize('url', ['file:///etc/hosts', 'http://127.0.0.1:8642/health', 'http://localhost:3000',
                                 'http://192.168.1.1/', 'http://100.71.37.12/', 'chrome://settings'])
def test_private_browser_refuses_host_and_lan(monkeypatch, url):
    from tools import browser_tool as bt
    monkeypatch.setattr(mwp, 'private_worker_active', lambda: True)
    r = json.loads(bt.browser_navigate(url, task_id='canary'))
    assert r['success'] is False and 'Blocked' in r['error']


def test_private_image_source_confined(monkeypatch, tmp_path):
    from tools import image_generation_tool as ig
    monkeypatch.setattr(mwp, 'private_worker_active', lambda: True)
    seen = {}
    import tools.image_source as src

    async def fake(s, task_id, permitted=('image',)):
        seen['src'] = s
        raise src.SourceUnsafe('outside workspace', src=s, origin='file')
    monkeypatch.setattr(src, 'resolve_local_source_to_data_url', fake)
    _, _, err = ig._confine_source_images('/Users/Chris/.hermes/auth.json', None, 'canary')
    assert err and seen['src'].endswith('auth.json')
