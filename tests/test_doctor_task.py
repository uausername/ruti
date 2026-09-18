"""The proxy task must not run elevated, and doctor must say how to undo it."""

from __future__ import annotations

import pytest

from ruti import doctor, proc

XML = """<?xml version="1.0" encoding="UTF-16"?>\r\r
<Task version="1.3">\r\r
  <Principals>\r\r
    <Principal id="Author">\r\r
      <UserId>S-1-5-21-1</UserId>\r\r
      <LogonType>InteractiveToken</LogonType>\r\r
      {run_level}\r\r
    </Principal>\r\r
  </Principals>\r\r
</Task>\r\r
"""


def _schtasks(run_level: str, ok: bool = True):
    def run(argv, **kwargs):
        assert argv[:2] == ["schtasks", "/Query"]
        # the \r\r\n endings are why progress stripping must stay off here
        assert kwargs.get("strip_progress") is False
        return proc.Result(argv, 0 if ok else 1, XML.format(run_level=run_level), "", 0.0)
    return run


@pytest.mark.parametrize("element, expected", [
    ("<RunLevel>HighestAvailable</RunLevel>", "HighestAvailable"),
    ("<RunLevel>LeastPrivilege</RunLevel>", "LeastPrivilege"),
    ("", "LeastPrivilege"),  # absent means the scheduler's default
])
def test_run_level_is_read_from_the_task_xml(monkeypatch, element, expected):
    monkeypatch.setattr(proc, "run", _schtasks(element))
    assert doctor._task_run_level() == expected


def test_an_unregistered_task_has_no_run_level(monkeypatch):
    monkeypatch.setattr(proc, "run", _schtasks("", ok=False))
    assert doctor._task_run_level() is None


def test_an_elevated_task_is_flagged_with_the_commands_to_undo_it(monkeypatch):
    monkeypatch.setattr(doctor, "_task_run_level", lambda: "HighestAvailable")
    check = doctor._check_proxy_task()
    assert check.status == doctor.WARN
    assert check.fix is None  # an elevated shell's job, spelled out rather than scripted
    assert "-RunLevel Limited" in check.detail

    monkeypatch.setattr(doctor, "_task_run_level", lambda: "LeastPrivilege")
    check = doctor._check_proxy_task()
    assert check.status == doctor.OK and check.fix is None


def test_when_the_task_cannot_deliver_nothing_else_is_launched(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return proc.Result(argv, 0, "", "", 0.0)

    monkeypatch.setattr(proc, "run", run)
    monkeypatch.setattr(doctor, "_wait_for_liveliness", lambda _budget: False)
    with pytest.raises(RuntimeError, match="by hand"):
        doctor._fix_proxy_start()
    # the task, and nothing else: no hidden or encoded PowerShell of its own
    assert calls == [["schtasks", "/Run", "/TN", doctor.SCHEDULED_TASK]]
