import importlib.util
import pathlib
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("console helper is Windows-only", allow_module_level=True)

_dir = pathlib.Path(__file__).resolve().parents[1] / "mods" / "ruti-seat"
sys.path.insert(0, str(_dir))
_spec = importlib.util.spec_from_file_location("ysk_forward", _dir / "ysk_forward.py")
ysk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ysk)


def test_a_permission_prompt_counts_as_a_dialog():
    lines = ["Bash command", "  rm -rf build", "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    assert ysk.dialog_up(lines)


def test_the_card_alone_is_not_a_dialog():
    assert not ysk.dialog_up(["✦ Heads up", "1: Understood  2: Chat in main session  0: Dismiss"])


def test_composer_text_needs_text_after_the_prompt_mark():
    assert ysk.composer_text(["╭────╮", "│ > Heads up: the cache costs more │", "╰────╯"])
    assert not ysk.composer_text(["│ >                                  │"])
