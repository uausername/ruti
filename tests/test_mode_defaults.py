"""`ruti defaults`: the modes a session has unless it says otherwise."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from ruti import cli, config, modes


def test_built_in_defaults_when_nothing_is_set():
    assert modes.current("s1") == modes.DEFAULTS
    assert modes.current(None) == modes.DEFAULTS


def test_a_default_reaches_a_session_that_never_set_the_mode():
    modes.set_defaults({"coding": "on", "free": "soft", "wait": "on"})
    state = modes.current("s1")
    assert state["coding"] is True and state["free"] == "soft" and state["wait"] is True
    assert modes.current(None)["free"] == "soft"


def test_a_sessions_own_setting_wins_over_the_default():
    modes.set_defaults({"free": "hard", "coding": "on"})
    modes.set_free("s1", "off")
    modes.set_coding("s1", False)
    assert modes.current("s1")["free"] == "off"
    assert modes.current("s1")["coding"] is False
    assert modes.current("s2")["free"] == "hard"


@pytest.mark.parametrize("pairs", [
    {"free": "sometimes"}, {"coding": "maybe"}, {"council": "yes"}, {"nonsense": "on"},
])
def test_invalid_defaults_are_refused(pairs):
    with pytest.raises(ValueError):
        modes.set_defaults(pairs)


def test_one_bad_pair_writes_none_of_them():
    with pytest.raises(ValueError):
        modes.set_defaults({"coding": "on", "free": "sometimes"})
    assert modes.user_defaults() == {}


def test_a_hand_edited_file_is_read_for_what_is_valid_in_it():
    config.write_json(modes.DEFAULTS_FILE, {"wait": True, "free": "sometimes", "bogus": 1})
    assert modes.user_defaults() == {"wait": True}


def test_a_corrupt_file_means_the_built_in_defaults():
    config.write_json(modes.DEFAULTS_FILE, [1, 2])
    assert modes.current("s1") == modes.DEFAULTS


def test_clear_one_and_clear_all():
    modes.set_defaults({"coding": "on", "wait": "on"})
    modes.clear_defaults(["coding"])
    assert modes.user_defaults() == {"wait": True}
    modes.clear_defaults()
    assert modes.user_defaults() == {}


def test_clearing_an_unknown_mode_is_an_error():
    with pytest.raises(ValueError):
        modes.clear_defaults(["nonsense"])


def run(*args):
    result = CliRunner().invoke(cli.main, ["defaults", *args])
    return result.exit_code, " ".join(result.output.split())


def test_cli_set_show_clear():
    code, out = run("set", "coding=on", "free=soft", "wait=on")
    assert code == 0 and "coding=on" in out and "free=soft" in out
    code, out = run("show")
    assert code == 0 and "coding : on set" in out and "council : off built-in" in out
    code, out = run("clear", "wait")
    assert code == 0 and modes.user_defaults() == {"coding": True, "free": "soft"}
    code, _ = run("clear")
    assert code == 0 and modes.user_defaults() == {}


@pytest.mark.parametrize("args", [("set", "coding"), ("set", "free=sometimes"),
                                  ("clear", "nonsense")])
def test_cli_rejects_bad_input_without_writing(args):
    code, _ = run(*args)
    assert code != 0 and modes.user_defaults() == {}


def test_cli_json():
    modes.set_defaults({"wait": "on"})
    result = CliRunner().invoke(cli.main, ["defaults", "--json"])
    assert result.exit_code == 0
    assert '"overridden"' in result.output and '"wait"' in result.output
