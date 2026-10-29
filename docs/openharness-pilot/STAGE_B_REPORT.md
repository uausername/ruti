# Stage B — independent Claude-manager → Codex-child (2026-10-29)

Status: **PARTIAL.** The live manager→child loop worked. Visual TUI proof, the Rust
build, Windows runtime, and any other mode beyond the footer were not verified.

## Platform / identity (real)
- Debian 13 on WSL2, Linux Claude 2.1.312 (Claude Pro login), Linux Codex 0.164.0
  (ChatGPT login), Harness 0.5.7. No API keys in the Linux environment; no API calls made.
- Manager: top-level Claude in tmux `ruti-manager`, `harness whoami` → can start,
  caller "outside Harness". Not launched via `harness new claude`.
- Worktree `~/work/ruti-statusline`, branch `pilot/statusline-codex` from `d5834a5`;
  user's uncommitted footer work carried via local snapshot commit `e97c021`
  (EOL-normalized; 84 of 90 "modified" files in the main checkout differ only by CRLF).
  Nothing pushed/merged; main checkout untouched; child changes left uncommitted.
- Linux venv `~/work/venv-ruti` (no sudo: venv --without-pip + get-pip.py).

## Live loop (real)
1. `harness new codex <worktree> --name ruti-statusline --mode ask -- <task>`.
2. Codex asked to trust the folder. Trust would be saved for the Git root
   `/mnt/c/mycode/ruti` (worktree resolves to main repo root). The user chose to trust
   the root and pressed it themselves in the child's tmux pane. Side effect: a persistent
   trust entry for the main checkout in `~/.codex/config.toml` (no backup was taken by me).
3. `harness wait` → `needs-input` for several minutes (person gate), then `idle`.
4. `harness show` returned the child's answer (13 tool calls).
5. Manager independently inspected the diff and re-ran tests (below).
6. `harness close` → **NOT_YOURS** ("only the person can"), although `show` says
   "started by Claude Code". Child left idle; the user must close it. Possible Harness
   identity quirk, unresolved. Fix cycles used: 0 of 2. Nested spawn attempts: none.

## Child's change (reviewed)
`ruti/footer.py`: cache-only read of `facts.json` (no HTTP/GPU/subprocess/write);
`unknown`/`stale`/`live` freshness; missing session id → `Ruti unknown`;
Claude-hook modes shown with `*` (wait, flow, manager, timemachine, eff+); one line ≤120
chars, control/ANSI stripped; exception → `Ruti unavailable`. +10 tests; docs/codex.md and
CODEX_STATUSLINE_NOTES.md updated.

Manager review notes:
- Trade-off: with no Claude statusline refresher, standalone Codex shows
  `proxy unknown local:unknown GPU unknown` indefinitely (functional regression vs the
  previous probing footer; honest, but a facts producer for Codex is still needed).
- Rust patch, installer and the Windows build were not touched or rebuilt.
- 5h/weekly Codex quota: no verified local producer → omitted (BLOCKED).
- Timemachine shows `timemachine*` by default (built-in default), not user choice.

## Tests (real, reproduced by manager)
- `compileall -q ruti`: ok. Full pytest Linux py3.13: **861 passed, 6 skipped, 14 errors,
  0 failures** (baseline 851/6/14; errors = repo `litellm/` dir shadowing the package in
  `test_proxy_callback.py`, `test_tools_probe.py`). `test_footer.py`: 16 passed.
  Evidence: `manager-pytest-linux.xml`, child's `codex-child-pytest.xml/.log`.
- CI matrix (Windows 3.12/3.13, Ubuntu 3.12) and Windows tests for this change: NOT RUN
  (stage A: 47 Windows unit tests, unrelated to this diff). No remote CI.

## Live footer isolation probe (real, isolated RUTI_HOME, Codex host)
Session A after `ruti off`, `mode coding on`, `mode free soft`:
`Ruti OFF coding free:soft timemachine* proxy unknown local:unknown GPU unknown`
Session B: `Ruti on timemachine* proxy unknown local:unknown GPU unknown` — no leakage.

## Mode matrix (honest, short)
| Function | Unit | Integration | Live | Result / note |
|---|---|---|---|---|
| off/on, session isolation | yes | – | footer A/B probe, Linux | PASS (footer). route/delegate blocking: only existing tests |
| coding, free | yes | – | CLI set + footer shown | PARTIAL: setting stored; Codex has no UserPromptSubmit hook |
| wait/flow/manager/timemachine/eff+ | existing tests | – | CLI accepts; footer shows `*` | PARTIAL: Claude-hook behavior not supported on Codex |
| council, jev | existing tests (mock) | – | CLI toggles only | NOT RUN beyond toggle; no paid calls |
| defaults, show/hide | existing tests | – | NOT RUN | NOT RUN |
| route/delegate/verdict/worktree apply | existing tests | – | `delegate` w/o args → usage error only | NOT RUN live |
| status/doctor/report | – | – | `doctor` run on Linux: 4 fixable warnings, no `--fix` | PARTIAL (Linux state is fresh/empty) |
| Visual footer in Codex TUI, Rust build | – | – | – | NOT RUN (BLOCKED BY NO BUILD on Linux) |

## HACCP readiness: **NO-GO (for rollout now); GO for a bounded second pilot**
Criteria: (met) two subscription CLIs connected on Linux, top-level Claude manager,
separate worktree, wait/show worked, no API keys/charges seen, main untouched.
(not met) live child repair loop, live review by a second Codex harness, child lifecycle
(close failed NOT_YOURS), human gates required (folder trust, and `--mode ask` approvals),
trust scope widened to the Git root, WSL reliability only seen over one short run,
Windows/WSL split (Ruti hooks, proxy/GPU reachability from Linux unverified),
TUI visual proof absent. Before GO: add a Codex facts producer, build/verify the patched
footer on Windows, run one full implement→independent-review→fix cycle, use a standalone
clone or Harness project folder to avoid Git-root trust, resolve harness close ownership.
