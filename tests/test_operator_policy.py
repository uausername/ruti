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
