import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(path):
    spec = importlib.util.spec_from_file_location(path.stem + "_policy_probe", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _check(mod):
    forbidden = set(mod.DEFAULT_FORBIDDEN_ACTIONS)
    assert not {"deploy", "restart", "config"} & forbidden
    assert {"external_send", "publish", "payment", "signature", "destructive",
            "credential_change", "privacy_wall_change"} <= forbidden
    allowed = set(mod.DEFAULT_ALLOWED_SIDE_EFFECTS)
    assert {"config_edit_with_backup", "deploy_via_existing_mechanism",
            "restart_via_external_activation", "scoped_tool_install"} <= allowed
    assert "architecture_production" not in Path(mod.__file__).read_text()


def test_live_helper_default_policy():
    _check(_load(ROOT / "scripts" / "safe_delegate_card.py"))


def test_parity_fixture_default_policy():
    _check(_load(ROOT / "tests" / "fixtures" / "margot_parity" / "safe_delegate_card.py"))
