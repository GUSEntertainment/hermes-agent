import subprocess, sys
from pathlib import Path

def test_member_delivery_fixture(tmp_path):
    root = Path(__file__).resolve().parents[2]
    r = subprocess.run([sys.executable, str(Path(__file__).with_name("member_delivery_fixture.py")), str(root), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAILS: []" in r.stdout
