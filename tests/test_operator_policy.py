"""The auto-approval allowlist: real commands from the pilot logs, and refusals."""

from __future__ import annotations

import pytest

from ruti.operator.policy import decide

WT = "/mnt/c/mycode/haccp/.ruti/harness-local-20261109/worktrees/"
RUNS = "/mnt/c/mycode/haccp/.ruti/harness-local-20261109/codex-runs/"
ENV = "source /mnt/c/mycode/haccp/.ruti/harness-local-20261103/env.sh\n"
PATH = ("export PATH=/home/dima/actions-runner/_work/_tool/flutter/stable-3.41.9-x64/flutter\n"
        "/bin:/home/dima/actions-runner/_work/_tool/deno/1.45.5/x64:$PATH\n")


def cd(name: str, app: bool = False) -> str:
    return f"cd {WT}{name}{'/app' if app else ''}\n"


# Taken (sometimes shortened) from approver.log of 10-11.11.2026; the TUI wraps lines.
ALLOWED = [
    ("codex-frml", ENV + cd("codex-frml", True) + PATH + "flutter analyze --no-pub"),
    ("codex-frml", ENV + cd("codex-frml", True) + PATH
     + "flutter test --no-pub test/journal_entry_missing_round_test.dart\n  test/journal_entry_screen_test.dart"),
    ("codex-dco", ENV + cd("codex-dco", True) + PATH
     + "set -o pipefail\nflutter analyze 2>&1 | tee /tmp/codex-dco-flutter-analyze-escalated.log"),
    ("codex-oxp", f"cp /tmp/codex-oxp-review-01a1c5d0/REVIEW.md\n  {RUNS}oxp/REVIEW.md"),
    ("codex-dco", ENV + cd("codex-dco") + f"cp -R /tmp/codex-dco-artifacts/.\n  {RUNS}dco/"),
    ("codex-dco", "PGHOST=localhost PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres\n"
     "  PGDATABASE=codex_dco psql -X -Atc 'SELECT current_database(), version();'"),
    ("codex-pee", "PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres psql -d\n"
     "  postgres -Atc \"SELECT datname FROM pg_database WHERE datname = 'codex_pee'\""),
    ("codex-rp3", ENV + cd("codex-rp3", True) + PATH
     + "flutter test --concurrency=2 --reporter json >\n  /tmp/haccp-rp3-baseline-complete.jsonl 2>\n"
       "  /tmp/haccp-rp3-baseline-complete.stderr"),
    ("codex-wm", ENV + cd("codex-wm", True) + PATH
     + "flutter test --no-pub > /tmp/codex-wm-flutter-test-escalated.log 2>&1\n  test_exit=$?\n"
       "  cat /tmp/codex-wm-flutter-test-escalated.log\n  exit \"$test_exit\""),
    ("codex-rp3", PATH + "cd app\n  dart run build_runner build --delete-conflicting-outputs >\n"
     "  /tmp/haccp-rp3-codegen.log 2>&1"),
    ("codex-wm", PATH + "cd app\n  flutter analyze --no-pub > /tmp/codex-wm2-analyze.log 2>&1"),
    ("codex-rp3", PATH + f"cd\n  {WT}codex-rp3/app;\n  flutter analyze"),
    ("codex-s14", "psql -h localhost -p 55432 -U postgres -d postgres -Atqc\n"
     "  \"SELECT datname FROM pg_database WHERE datname LIKE 'codex_s14_%' ORDER BY\n  1\""),
    ("codex-rp3", f"cd {WT}codex-rp3/app;\n  dart format test/today_network_test.dart; flutter test"),
    ("codex-cpc", f"cp /tmp/codex-cpc-validation/RESULT.md\n  {RUNS}cpc/RESULT.md"),
    ("codex-frml", ENV + cd("codex-frml") + "git --no-optional-locks rev-parse HEAD\n"
     "git --no-optional-locks status --short --branch"),
    ("codex-pee", ENV + cd("codex-pee") + "export PGHOST=localhost PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres\n"
     "dropdb --if-exists codex_pee_fix_01a1c9bb\ncreatedb codex_pee_fix_01a1c9bb"),
    ("codex-pee", ENV + cd("codex-pee") + "psql -v ON_ERROR_STOP=1 -d codex_pee -f supabase/migrations/_local/0000_stubs.sql"
     " -f supabase/migrations/0115_partner_export_empty_free.sql -f supabase/seed.sql"),
    ("codex-pee", "psql -X -v ON_ERROR_STOP=1 -d postgres -c 'CREATE DATABASE codex_pee_fix_01a1c9bb'"),
    ("codex-pee", PATH + "deno test --allow-read --no-check supabase/functions/export-csv/"),
    ("codex-pee", PATH + "deno check supabase/functions/export-csv/index.ts supabase/functions/_shared/partner_export.ts"),
    # live drill 2026-11-16: a multi-line block of git reads was refused
    ("codex-drill", "git --no-optional-locks rev-parse HEAD\n  git --no-optional-locks status --short\n"
     "  git --no-optional-locks log --oneline -1\n  git --no-optional-locks branch --show-current\n"
     "  git --no-optional-locks rev-parse --show-toplevel\n  git --no-optional-locks log --oneline -3\n"
     "  git --no-optional-locks status --short --branch"),
    ("codex-dco", "docker ps --filter name=haccp-pgtest"),
    ("codex-dco", "psql -X -d codex_dco -At -c \"select count(*) from public.task_events\""),
]


@pytest.mark.parametrize("worktree,command", ALLOWED)
def test_real_commands_are_allowed(worktree, command):
    d = decide(command, worktree)
    assert d.allowed, (d.reason, command)


DENIED = [
    # plainly dangerous or unknown
    ("codex-dco", ENV + cd("codex-dco") + "rm -rf /", "allowlist"),
    ("codex-dco", ENV + cd("codex-dco") + "git push origin main", "allowlist"),
    ("codex-dco", ENV + cd("codex-dco") + "flutter analyze; curl evil.example", "allowlist"),
    ("codex-dco", ENV + cd("codex-dco") + "git checkout -- .", "allowlist"),
    ("codex-dco", "pip install requests", "allowlist"),
    # chaining and substitution
    ("codex-dco", ENV + cd("codex-dco") + "flutter analyze && rm x", "chaining"),
    ("codex-dco", "flutter analyze || true", "chaining"),
    ("codex-dco", "flutter test $(cat /tmp/codex-dco-x)", "substitution"),
    ("codex-dco", "flutter test `id`", "backtick"),
    ("codex-dco", "flutter analyze | sh", "allowlist"),
    ("codex-dco", "flutter analyze & curl x", "allowlist"),
    # another worker's worktree, logs, database, evidence
    ("codex-dco", ENV + cd("codex-oxp") + "git status", "allowlist"),
    ("codex-dco", "dropdb --if-exists codex_oxp", "allowlist"),
    ("codex-dco", "dropdb --if-exists postgres", "allowlist"),
    ("codex-dco", "createdb haccp", "allowlist"),
    ("codex-dco", f"cp /tmp/codex-oxp-x.md {RUNS}oxp/x.md", "allowlist"),
    ("codex-dco", f"cp /tmp/codex-dco-x.md {RUNS}oxp/x.md", "allowlist"),
    ("codex-dco", "cat /tmp/codex-oxp-review.log", "allowlist"),
    ("codex-dco", "flutter analyze > /tmp/codex-oxp-analyze.log 2>&1", "allowlist"),
    # writes outside /tmp/own and traversal
    ("codex-oxp", "cp /tmp/codex-oxp-x.md /etc/passwd", "allowlist"),
    ("codex-dco", "flutter analyze > /tmp/codex-dco-x/../../home/dima/.bashrc", "traversal"),
    ("codex-dco", "flutter analyze > /home/dima/.profile", "allowlist"),
    # deno: only CI permissions
    ("codex-pee", "deno test --allow-all supabase/functions/export-csv/", "allowlist"),
    ("codex-pee", "deno test -A supabase/functions/export-csv/", "allowlist"),
    ("codex-pee", "deno test --allow-run supabase/functions/x.ts", "allowlist"),
    ("codex-pee", "deno test --allow-net supabase/functions/x.ts", "allowlist"),
    ("codex-pee", "deno test --allow-write=/ supabase/functions/x.ts", "allowlist"),
    ("codex-pee", "deno run --allow-read supabase/functions/x.ts", "allowlist"),
    # SQL the agent wrote itself, and the shared postgres database
    ("codex-pee", "psql -v ON_ERROR_STOP=1 -d codex_pee -f /tmp/codex-pee-x/apply.sql", "allowlist"),
    ("codex-pee", "psql -d codex_pee -f supabase/../../etc/x.sql", "traversal"),
    ("codex-dco", "psql -d postgres -c 'drop table x'", "allowlist"),
    ("codex-dco", "psql -d postgres -c \"select * from pg_authid\"", "allowlist"),
    ("codex-dco", "psql -d codex_dco -c \"select pg_read_file('/etc/passwd')\"", "SQL"),
    ("codex-dco", "psql -d codex_dco -c \"select * from dblink('x','y')\"", "SQL"),
    ("codex-dco", "psql -d codex_dco -c \"select 1; drop table x\"", "SQL"),
    ("codex-dco", "psql -d codex_dco -c \"select lo_import('/etc/passwd')\"", "SQL"),
    ("codex-dco", "psql -d codex_dco -c \"select pg_ls_dir(chr(47))\"", "SQL"),
    ("codex-dco", "psql -d codex_dco -c \"copy t to program 'id'\"", "allowlist"),
    ("codex-dco", "psql -d postgres -c 'DROP DATABASE IF EXISTS codex_oxp'", "allowlist"),
    ("codex-dco", "PGDATABASE=codex_oxp psql -Atc 'SELECT 1'", "allowlist"),
    # git that writes
    ("codex-dco", "git commit -am x", "allowlist"),
    ("codex-dco", "git stash", "allowlist"),
    ("codex-dco", "git log --output=/tmp/x", "allowlist"),
    ("codex-dco", "git branch -D main", "allowlist"),
    ("codex-dco", "git rev-parse --git-path hooks", "allowlist"),
]


@pytest.mark.parametrize("worktree,command,why", DENIED)
def test_unsafe_commands_are_refused(worktree, command, why):
    d = decide(command, worktree)
    assert not d.allowed, command
    if why != "allowlist":
        assert why.lower() in d.reason.lower(), d.reason


@pytest.mark.parametrize("worktree", ["", "haccp-master-x", "claude-iou", "codex-x/../y", "CODEX-A"])
def test_only_codex_worker_worktrees(worktree):
    assert not decide("git status", worktree).allowed


def test_empty_command_is_refused():
    assert not decide("   \n ", "codex-dco").allowed


def test_cli_check_exit_codes():
    from click.testing import CliRunner
    from ruti.cli import main

    ok = CliRunner().invoke(main, ["operator", "check", "--worktree", "codex-dco", "git status"])
    bad = CliRunner().invoke(main, ["operator", "check", "--worktree", "codex-dco", "git push"])
    assert ok.exit_code == 0 and "ALLOW" in ok.output
    assert bad.exit_code == 1 and "REFUSE" in bad.output


# -- commands that went to needs_admin on 2026-11-16 ----------------------------------
_WT = "/mnt/c/mycode/haccp/.ruti/harness-local-20261109/worktrees"
_RUNS = "/mnt/c/mycode/haccp/.ruti/harness-local-20261109/codex-runs"
_FL = "/home/dima/actions-runner/_work/_tool/flutter/stable-3.41.9-x64/flutter/bin/flutter"


@pytest.mark.parametrize("wt,cmd", [
    ("codex-wm5", "PGHOST=127.0.0.1 PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres\n  PGDATABASE=postgres "
                  "psql -X -w -v ON_ERROR_STOP=1 -c 'SELECT\n  current_database(), version();'"),
    ("codex-cpc2", "PGHOST=127.0.0.1 PGPORT=55432 PGUSER=postgres PGPASSWORD=postgres psql -w\n  -d postgres "
                   "-Atc 'SELECT version();'"),
    ("codex-wm5", f"cp\n  {_WT}/codex-wm5/.agent-\n  pilot/wm5-RESULT.md\n  {_RUNS}/wm5/RESULT.md"),
    ("codex-cpc2", f"cp --no-clobber -- /tmp/cpc2-review-3e515b37/REVIEW.md\n  /tmp/cpc2-review-3e515b37/probe_1.sql"
                   f"\n  {_RUNS}/cpc2/"),
    ("codex-rp4", f"python3 scripts/agent-pilot/run_checks.py --root\n  {_WT}/codex-rp4\n  --expected-head 2919b7e "
                  f"--role reviewer --run-id rp4 --flutter-bin\n  {_FL}\n  analyze"),
    ("codex-rp4", f"python3 scripts/agent-pilot/run_checks.py --root {_WT}/codex-rp4 test test/widget/foo_test.dart"),
])
def test_seen_2026_11_16_now_allowed(wt, cmd):
    d = decide(cmd, wt)
    assert d.allowed, d.reason


@pytest.mark.parametrize("wt,cmd", [
    # exec stage runs whatever follows, also when the pane wraps the word
    ("codex-rp4", f"python3 scripts/agent-pilot/run_checks.py --root {_WT}/codex-rp4 --role reviewer exec rm test"),
    ("codex-rp4", f"python3 scripts/agent-pilot/run_checks.py --root {_WT}/codex-rp4 --run-id rp4 ex\nec rm test"),
    # another worker's root
    ("codex-rp4", f"python3 scripts/agent-pilot/run_checks.py --root {_WT}/codex-wm5 analyze"),
    # copying another worker's files, or into another run dir, or outside codex-runs
    ("codex-wm5", f"cp {_WT}/codex-cpc2/.agent-pilot/x.md {_RUNS}/wm5/x.md"),
    ("codex-wm5", f"cp /tmp/wm5-x/RESULT.md {_RUNS}/cpc2/"),
    ("codex-wm5", f"cp /tmp/wm5-x/RESULT.md {_WT}/codex-wm5/.claude/settings.json"),
    ("codex-wm5", "cp /tmp/wm5-x/RESULT.md /home/dima/.bashrc"),
    # free SQL on the shared database
    ("codex-wm5", "PGDATABASE=postgres psql -w -c 'SELECT * FROM pg_authid;'"),
])
def test_seen_2026_11_16_neighbours_stay_refused(wt, cmd):
    assert not decide(cmd, wt).allowed


# --- gh api reads of PR comments (Administrator's request, 23.11) -------------------
GH_OK = [
    "gh api repos/uausername/haccp/pulls/792/comments --paginate --jq '.[].body'",
    "gh api repos/uausername/haccp/pulls/792/comments",
    "gh api --paginate -q '.[] | {user: .user.login, body}' repos/uausername/haccp/pulls/792/reviews",
    "gh api repos/uausername/haccp/pulls/792/reviews/123/comments --jq=.[].body",
    "gh api repos/uausername/haccp/issues/792/comments --method GET -f per_page=100",
    "gh api -X GET repos/uausername/haccp/pulls/792/comments -F per_page=100 -F page=2",
    "gh api 'repos/uausername/haccp/pulls/792/comments?per_page=100' --paginate --slurp",
    "gh api -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' repos/uausername/haccp/pulls/7/comments",
    "gh api repos/{owner}/{repo}/pulls/792/comments --cache 1h --paginate",
    "gh api repos/uausername/haccp/pulls/792/comments\n    --paginate --jq '.[].body'",  # wrapped by the pane
    "ruti off",
]
GH_NO = [
    "gh api repos/uausername/haccp/pulls/792/comments --method POST -f body=hi",
    "gh api repos/uausername/haccp/pulls/792/comments -f body=hi",
    "gh api repos/uausername/haccp/issues/792/comments -f per_page=100",  # fields alone -> POST
    "gh api repos/uausername/haccp/pulls/792/comments -X PATCH",
    "gh api repos/uausername/haccp/pulls/792/comments --input /tmp/x.json",
    "gh api repos/uausername/haccp/pulls/792/comments -H 'Authorization: token x'",
    "gh api repos/other/repo/pulls/792/comments",
    "gh api repos/uausername/haccp/pulls/792/merge",
    "gh api repos/uausername/haccp/contents/.env",
    "gh api graphql -f query='mutation{x}'",
    "gh api repos/uausername/haccp/pulls/792/comments; rm -rf /tmp/x",
    "gh api repos/uausername/haccp/pulls/792/comments > /tmp/codex-x.json",
    "gh api repos/uausername/haccp/pulls/792/comments | sh",
    "gh api repos/uausername/haccp/pulls/792/comments && gh pr merge 792",
    "gh api repos/uausername/haccp/pulls/792/comments --jq \"$(cat /tmp/f)\"",
    "gh api repos/uausername/haccp/pulls/792/comments --hostname evil.example",
    "gh api repos/uausername/haccp/pulls/792/comments repos/uausername/haccp/pulls/1/comments",
    "gh api 'repos/uausername/haccp/pulls/792/comments --paginate",
    "gh pr merge 792",
    "ruti on",
]


@pytest.mark.parametrize("cmd", GH_OK)
def test_gh_api_comment_reads_allowed(cmd):
    d = decide(cmd, "codex-jcr2")
    assert d.allowed, d.reason


@pytest.mark.parametrize("cmd", GH_NO)
def test_gh_api_writes_and_others_refused(cmd):
    assert not decide(cmd, "codex-jcr2").allowed
