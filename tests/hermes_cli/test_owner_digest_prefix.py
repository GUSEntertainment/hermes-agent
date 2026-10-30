import hashlib

from hermes_cli import owner_cell as oc


def test_prefixed_and_bare_digest_both_match(tmp_path):
    p = tmp_path / "ready.txt"
    p.write_bytes(b"ready\n")
    hexd = hashlib.sha256(b"ready\n").hexdigest()
    assert oc.digest_matches(p, hexd)
    assert oc.digest_matches(p, "sha256:" + hexd)
    assert oc.digest_matches(p, "SHA256:" + hexd.upper())
    assert not oc.digest_matches(p, "sha256:" + "0" * 64)
    assert not oc.digest_matches(p, "")
