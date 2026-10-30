from types import SimpleNamespace
import pytest
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter,_PROFILE_REJECTED

def test_api_only_profile_does_not_modify_gateway_profiles(monkeypatch):
    adapter=APIServerAdapter(PlatformConfig(enabled=True,extra={'profile_allowlist':['connor']}))
    cfg=SimpleNamespace(multiplex_profiles=True,multiplex_profile_allowlist=['joshgarcia'])
    adapter.gateway_runner=SimpleNamespace(config=cfg)
    seen=[]
    def profiles(*,multiplex,profile_allowlist):
        seen.append(profile_allowlist)
        return [(p,object()) for p in profile_allowlist]
    monkeypatch.setattr('hermes_cli.profiles.profiles_to_serve',profiles)
    assert adapter._resolve_request_profile(SimpleNamespace(match_info={'profile':'connor'}))=='connor'
    assert cfg.multiplex_profile_allowlist==['joshgarcia']
    assert seen==[['joshgarcia','connor']]
    assert adapter._resolve_request_profile(SimpleNamespace(match_info={'profile':'unknown'})) is _PROFILE_REJECTED

def test_malformed_extra_profiles_fail_closed():
    with pytest.raises(ValueError):APIServerAdapter(PlatformConfig(enabled=True,extra={'profile_allowlist':['../default']}))
