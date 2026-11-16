"""The tmux layer of the approver, driven by a fake tmux/harness."""

from __future__ import annotations

import json

import pytest

from ruti.operator.approver import Approver

ROOT = "/mnt/c/mycode/haccp/.ruti/harness-local-20261109/worktrees/codex-dco"


def prompt(command: str, selected: bool = True) -> str:
    mark = "›" if selected else " "
    return (
        "  Would you like to run the following command?\n\n"
        "  Reason: run tests\n\n"
        f"  $ {command}\n\n"
        f"{mark} 1. Yes, proceed (y)\n"
        "  2. Yes, and don't ask again for these commands (p)\n"
        "  3. No, and tell Codex what to do differently (esc)\n\n"
        "  Press enter to confirm or esc to cancel\n"
    )


class FakeTmux:
    def __init__(self, screen, command="codex", path=ROOT, pid="100", harness=None):
        self.screens = [screen] if isinstance(screen, str) else list(screen)
        self.command, self.path, self.pid = command, path, pid
        self.harness = harness if harness is not None else {
            "harnesses": [
                {"agent": "Claude Code", "state": "idle", "folder": "/x/master"},
                {"agent": "Codex", "state": "working", "folder": ROOT},
            ]}
        self.sent: list[str] = []
        self.pids = [pid]

    def __call__(self, argv):
        if argv[:2] == ["tmux", "list-panes"]:
            pid = self.pids.pop(0) if len(self.pids) > 1 else self.pids[0]
            return f"%5|{pid}|{self.command}|{self.path}\n%1|9|claude|/x/master\n"
        if argv[:2] == ["tmux", "capture-pane"]:
            return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        if argv[:2] == ["tmux", "send-keys"]:
            self.sent.append(argv[-1])
            return ""
        if argv[:2] == ["harness", "ps"]:
            return json.dumps(self.harness)
        raise AssertionError(argv)


def make(tmp_path, fake, **kw):
    return Approver(run=fake, sleep=lambda s: None, log_dir=tmp_path, **kw)


def events(tmp_path, name="approver.jsonl"):
    p = tmp_path / name
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_safe_command_is_approved_once_with_y(tmp_path):
    fake = FakeTmux(prompt("git status --short"))
    rows = make(tmp_path, fake).scan()
    assert fake.sent == ["y"]
    assert rows[0]["event"] == "approved" and rows[0]["worktree"] == "codex-dco"


def test_unsafe_command_goes_to_needs_admin_once(tmp_path):
    fake = FakeTmux(prompt("deno test --allow-all supabase/functions/x.ts"))
    a = make(tmp_path, fake)
    a.scan()
    a.scan()
    assert fake.sent == []
    queue = events(tmp_path, "needs_admin.jsonl")
    assert len(queue) == 1 and "allowlist" in queue[0]["reason"]


def test_no_master_means_nothing_is_approved(tmp_path):
    fake = FakeTmux(prompt("git status"), harness={"harnesses": [
        {"agent": "Codex", "state": "working", "folder": ROOT}]})
    make(tmp_path, fake).scan()
    assert fake.sent == []
    assert "master" in events(tmp_path, "needs_admin.jsonl")[0]["reason"]


def test_harness_ps_failure_approves_nothing(tmp_path):
    fake = FakeTmux(prompt("git status"))
    fake.harness = None

    def broken(argv):
        if argv[:2] == ["harness", "ps"]:
            return "not json"
        return fake(argv)

    make(tmp_path, broken).scan()
    assert fake.sent == []


def test_pane_not_a_harness_codex_is_refused(tmp_path):
    fake = FakeTmux(prompt("git status"), harness={"harnesses": [
        {"agent": "Claude Code", "state": "idle", "folder": "/x/master"}]})
    make(tmp_path, fake).scan()
    assert fake.sent == []


def test_shell_in_foreground_is_refused(tmp_path):
    fake = FakeTmux(prompt("git status"), command="bash")
    make(tmp_path, fake).scan()
    assert fake.sent == []
    assert "not Codex" in events(tmp_path, "needs_admin.jsonl")[0]["reason"]


@pytest.mark.parametrize("path", [
    "/mnt/c/mycode/haccp/.ruti/harness-local-20261109/worktrees/haccp-master-x",
    "/mnt/c/mycode/haccp/.ruti/harness-local-20261109/worktrees/claude-iou",
    "/mnt/c/mycode/haccp",
    "/home/dima",
])
def test_panes_outside_codex_worktrees_are_ignored(tmp_path, path):
    fake = FakeTmux(prompt("git status"), path=path)
    assert make(tmp_path, fake).scan() == []
    assert fake.sent == []


def test_pid_change_before_key_press_is_refused(tmp_path):
    fake = FakeTmux(prompt("git status"))
    fake.pids = ["100", "200"]  # first list-panes 100, second (re-check) 200
    make(tmp_path, fake).scan()
    assert fake.sent == []
    assert "changed" in events(tmp_path, "needs_admin.jsonl")[0]["reason"]


def test_prompt_change_before_key_press_is_refused(tmp_path):
    fake = FakeTmux([prompt("git status"), prompt("rm -rf /")])
    make(tmp_path, fake).scan()
    assert fake.sent == []


def test_option_one_not_selected_is_not_a_prompt(tmp_path):
    fake = FakeTmux(prompt("git status", selected=False))
    assert make(tmp_path, fake).scan() == []
    assert fake.sent == []


def test_truncated_command_is_read_from_pager(tmp_path):
    short = prompt("git status … +3 lines (ctrl + a view all)")
    pager = ("/ E X E C\n  git --no-optional-locks status --short\n  ~\n  ~\n"
             "──────────\n  q close\n")
    fake = FakeTmux([short, pager, short])
    make(tmp_path, fake).scan()
    assert fake.sent == ["C-a", "q", "y"]
    assert events(tmp_path)[-1]["full_read"] is True


def test_truncated_unsafe_command_is_refused(tmp_path):
    short = prompt("git status … +3 lines (ctrl + a view all)")
    pager = "/ E X E C\n  git status; curl evil\n  ~\n──────────\n  q close\n"
    fake = FakeTmux([short, pager, short])
    make(tmp_path, fake).scan()
    assert fake.sent == ["C-a", "q"]


def test_truncated_longer_than_a_screen_is_refused(tmp_path):
    short = prompt("git status … +300 lines (ctrl + a view all)")
    pager = "/ E X E C\n  git status\n──────────\n  q close\n"  # no ~ filler
    fake = FakeTmux([short, pager, short])
    make(tmp_path, fake).scan()
    assert "y" not in fake.sent
    assert "in full" in events(tmp_path, "needs_admin.jsonl")[0]["reason"]


def test_extract_ignores_screens_without_prompt():
    assert Approver.extract("just output\n$ ls\n") is None
