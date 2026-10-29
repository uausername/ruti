import json
import sys
import tomllib
from pathlib import Path

import pytest

from ruti import codex_footer_install as install


@pytest.mark.parametrize("before", [
    'model = "x"\n',
    '[tui]\nanimations = false\n[tui.other]\nvalue = "stay"\n',
    '[tui]\nstatus_line = ["model", "weekly-limit"] # keep comment\n',
    '[tui]\nstatus_line = [\n "model", # ] in a comment\n "git-branch",\n]\n[other]\nx=1\n',
])
def test_preserves_other_config_values_and_existing_footer_order(before):
    after, original = install.configure_footer(before)
    parsed = tomllib.loads(after)
    assert parsed["tui"]["status_line"] == ["ruti", *original]
    old = tomllib.loads(before)
    old.setdefault("tui", {})["status_line"] = ["ruti", *original]
    assert parsed == old
    assert install.configure_footer(after)[0] == after


def test_tampered_binary_is_refused_before_any_installation(tmp_path, monkeypatch):
    binary = tmp_path / "codex.exe"
    binary.write_bytes(b"unexpected binary")
    manifest = tmp_path / "artifact.json"
    manifest.write_text(json.dumps({"source_tag": "rust-v0.164.0", "binary_sha256": "wrong"}))
    state = tmp_path / "state"
    monkeypatch.setattr(install, "STATE_ROOT", state)
    monkeypatch.setattr(sys, "argv", ["install", "--binary", str(binary), "--manifest", str(manifest),
                                     "--stock", str(binary), "--apply"])
    with pytest.raises(ValueError, match="manifest"):
        install.main()
    assert not state.exists()


def test_user_path_edit_keeps_other_entries_and_is_idempotent():
    directory = Path("C:/Ruti/codex")
    before = "C:/Other;C:/RUTI/codex/;C:/Stock"
    after = install.prepend_user_path(before, directory)
    assert after.split(";") == [str(directory), "C:/Other", "C:/Stock"]
    assert install.prepend_user_path(after, directory) == after
