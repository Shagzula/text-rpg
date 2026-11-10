"""The vendored Story Designer content (code/content/) is valid and in sync with upstream."""
import os
import subprocess
import sys

import check_content_drift as drift_mod  # noqa: E402  (tools/ on sys.path via conftest)
import validate_content


def test_vendored_content_validates():
    r = validate_content.validate(os.path.join(drift_mod.ROOT, "content"), out=lambda *_: None)
    assert r["ok"], r["failures"]
    assert r["templates"] == 35 and r["proposals"] >= 20
    assert {"npc_example_gatekeeper", "npc_mara", "npc_dozing_guard"} <= set(r["personas"])


def test_validator_cli_runs():
    p = subprocess.run([sys.executable, os.path.join(drift_mod.ROOT, "tools", "validate_content.py"), "-q"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_validator_flags_a_bad_item_name(tmp_path):
    import shutil
    d = tmp_path / "content"
    shutil.copytree(os.path.join(drift_mod.ROOT, "content"), d)
    y = d / "items_tier1.yaml"
    y.write_text(y.read_text().replace("name: Hempen Rope", "name: Rope (50 ft)"))
    r = validate_content.validate(str(d), out=lambda *_: None)
    assert not r["ok"]


def test_vendored_content_has_not_drifted_from_upstream():
    """Flags drift from /home/box/rpg-project/content (override with RPG_CONTENT_UPSTREAM).
    If the upstream directory isn't on this machine there's nothing to compare: that passes,
    since the vendored copy is the one the tests use. Fix drift with
    `python3 tools/check_content_drift.py --sync` and commit."""
    d = drift_mod.drift()
    if d is None:
        return
    assert not any(d.values()), f"vendored content/ drifted from {drift_mod.UPSTREAM}: {d}"


def test_drift_detector_sees_changes(tmp_path):
    import shutil
    up = tmp_path / "up"
    shutil.copytree(os.path.join(drift_mod.ROOT, "content"), up)
    (up / "README.md").write_text("changed")
    (up / "new.md").write_text("x")
    d = drift_mod.drift(str(up))
    assert d["changed"] == ["README.md"] and d["added"] == ["new.md"]
