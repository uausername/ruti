# Codex status line: local implementation evidence

Scope: the reviewed `codex/patches/statusline-ruti-0.164.0.patch` targets
`rust-v0.164.0`; these are not claims about every Codex release. Local `codex --help`
confirms TOML/dotted `--config` overrides but does not document a status command.
No model turn or network request was used for this review.

Codex's `[tui].status_line` selects and orders named native items via `/statusline`.
The patch adds `StatusLineItem::Ruti` to that enum, its preview and renderer. This
is not Claude's `statusLine.command`: that command receives JSON on stdin on UI
repaints. `ruti.statusline.main` captures Claude rate limits into `quota.json`;
`ruti.quota` explicitly describes those as Claude subscription counters. A list of
Codex native item names cannot run an arbitrary external command or provide that
Claude payload. The installer validates `tui.status_line` as a list of strings.

The repo uses a patched native item plus an external Python view:
`ruti_status::invoke` runs the installed absolute Python with
`-I -X utf8 -m ruti.footer --session-id <raw thread ID>`. It polls every five
seconds with a four-second process timeout, bounded stdout, and rejects late
results from another thread. The normal external `ruti status --json` view also
exists; it is not a stock Codex status-command integration. No new external pane
or watcher is implemented here.

PARTIAL: the Python footer reads machine-wide `facts.json` without refreshing it,
so it cannot stall the UI on HTTP, GPU subprocesses, or cache-write retries.
The existing `ruti.statusline._refresh_facts` producer can populate this cache;
standalone Codex currently has no independent background facts producer. Absent,
undated, invalid and expired observations render `unknown` or `stale`, rather than
healthy badges or fabricated measurements. Legacy empty loaded-model lists are
`unknown`: the producer also writes an empty list after a failed probe, so they
cannot establish that no model is loaded. Facts expire at `FACTS_TTL_SECONDS`.

Session records and delegates use exact namespaced IDs (`codex:<thread ID>`);
Claude's raw IDs cannot supply Codex modes, OFF state or delegates. Explicit
`render` IDs are already namespaced; the CLI converts its raw ID through
`sessions.current_session_id`. Missing identity shows `Ruti unknown` and no modes.
`modes.current` intentionally layers user/built-in defaults beneath this session's
overrides (including the built-in enabled `timemachine`). These are defaults, not
state copied from another session. `Ruti OFF` reports this session's kill switch.

PARTIAL: enabled `wait*`, `flow*`, `manager*`, `timemachine*`, and `eff+*` are stored
mode settings only. `*` means their Claude-hook automatic behavior is unsupported
on Codex. Displaying them does not implement pausing, continuation, seat changes,
letters, or effort changes. Coding/free are session settings, not evidence that
Codex automatically obeys a routing policy.

BLOCKED: this repo has no verified local Codex subscription-metric producer for
the Python footer. It omits 5h/weekly values and never reads Claude quota. Codex's
own native context/usage-limit items remain responsible for those measurements.
`quota.load` already returns an unknown snapshot on the Codex host.

PARTIAL: Python tests verify rendering and session isolation; no custom Rust build,
installation, live TUI, or Windows runtime verification is claimed in this task.
Output is at most 120 characters, one line, without ANSI/control characters;
unexpected read/render errors return `Ruti unavailable`. Local file reads still
depend on filesystem responsiveness; the patched process timeout is the outer bound.

## Validation in this worktree

`RUTI_HOME=$(mktemp -d)` was used for each test invocation.
`python -m compileall -q ruti` succeeded; footer tests: 16 passed.
Full pytest outside the socket-restricting sandbox: **861 passed, 6 skipped,
14 errors**, zero failures. Supplied pre-change baseline: **851 passed, 6 skipped,
14 errors**. The 14 errors are the known missing LiteLLM imports in
`tests/test_proxy_callback.py` and `tests/test_tools_probe.py`.
The first sandboxed run had 856 passed and five socket-permission failures;
rerunning with permission for local fake HTTP servers resolved all five.

JUnit output is `docs/openharness-pilot/codex-child-pytest.xml` and full output is
`docs/openharness-pilot/codex-child-pytest.log`. The task requested
`/home/dima/work/out/codex-child-pytest.xml`, but its hard rule prohibits editing
outside this worktree, so the report is left here for the manager to collect.
