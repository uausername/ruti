# Operator services

Deterministic replacements for what an LLM "Administrator" session used to do by hand
for the Harness → master Claude → Codex chain. They run in WSL next to Harness.

## Auto-approver — `ruti operator approve`

Answers Codex "Would you like to run the following command?" prompts with `y`
(option 1, approve **once**) when, and only when:

1. the tmux pane's cwd is a worker worktree `…/haccp/.ruti/harness-local-N/worktrees/codex-*`;
2. the pane's foreground program is Codex;
3. `harness ps --json` lists a Codex harness in that folder **and** a live master
   (Claude Code) harness — with no master, nothing is approved;
4. the command, shown in full (a truncated one is read from Codex's `Ctrl+A` pager),
   passes `ruti/operator/policy.py`;
5. right before the key press the pane has the same PID and the same prompt.

It never chooses "don't ask again". Every decision is a JSON line in
`$STATE_ROOT/operator/approver.jsonl`; refusals are also queued in
`$STATE_ROOT/operator/needs_admin.jsonl` for a person or the Administrator.

The allowlist is tied to the pane's own worker (`codex-dco` → `/tmp/codex-dco-*`,
databases `codex_dco*`, `codex-runs/dco/`) and follows the project's CI flags:
`deno test --allow-read|--allow-env|--no-check` only, SQL only from `supabase/` files,
read-only `SELECT` only against the worker's own database. Briefs for Codex must use
the same commands (see `docs/openharness-pilot/CODEX_WORKER_BRIEF_TEMPLATE.md`),
otherwise each such command waits for a person.

Check one command without a terminal:

```bash
ruti operator check --worktree codex-dco "deno test --allow-read --no-check supabase/functions/x/"
```

Run it supervised (survives crashes and WSL restarts):

```bash
cp ops/systemd/ruti-operator-approver.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ruti-operator-approver
loginctl enable-linger "$USER"
```

Codex 0.166 cuts a long command as `[… N lines] ^a view all`; the approver opens the
pager and reads it from the top one line at a time until it shows 100%, and refuses
if any step moves the view by more than one line.

## Trusted guard — `ruti operator guard`

```bash
ruti operator guard sync            # codex_guard.py + protect_rules.py from haccp@main
ruti operator guard install-hook    # one PreToolUse entry in ~/.codex/hooks.json
ruti operator guard status          # intact? current with main? wired in? (exit 1 if not)
```

The guard files are copied from `git show <commit>:scripts/agent-pilot/...` — the
committed LF bytes — into `~/.local/share/ruti/codex-guard/` (outside every worktree,
read-only) with a manifest of their sha256. A small `shim.py` finds the checkout from
the hook payload's `cwd`, and for checkouts under the repository runs the trusted copy
against it; anything else (another repository) is left alone, and any failure denies.

The hook goes first in `PreToolUse`: ruti's own Codex installer keeps foreign groups
first and appends its handlers, so the index Codex keys its trust by stays put. A person
trusts it **once** in Codex `/hooks`; worktrees no longer need `.codex/hooks.json` or
their own trust. After `main` changes the guard, `guard status` reports `current: false`
and `prepare` refuses until `guard sync` (the hook definition, and so its trust, does
not change). The approver never answers Codex's "Hooks need review" screen; it queues
it in `needs_admin.jsonl`.

## Quota — `ruti operator quota`

One answer for "may one more Codex worker start?" (exit 1 if not), with the readings:

* Claude — the freshest `quota.json` (WSL or Windows), the waker's limits;
* Codex — the freshest `rate_limits` record among recent rollouts of any Codex session
  (`~/.codex/sessions` in WSL and Windows; all models share one limit): no new worker at
  5h ≥ 70% or week ≥ 85%;
* at most `--max-parallel` (default 4) Codex harnesses alive.

`prepare` runs this gate. When it blocks on Codex, the master records
`ruti operator resume-at --after codex --checkpoint …` and the waker brings it back at
the reset.

## Worker worktrees — `ruti operator prepare`

```bash
ruti operator prepare codex-pee --model gpt-6.1-sol --effort high [--branch feat/x] [--base main]
```

Creates `.ruti/harness-local-<newest>/worktrees/codex-pee` with Windows git and
`core.autocrlf=false` (LF as committed, as CI sees it), rewrites `.git` to a relative
`gitdir` (Windows git and WSL git both read it — `gitdir: C:/…` made every git command
Codex ran in WSL fail), writes `.codex/config.toml` with the profile (excluded from
`git status`), checks `git status` is clean, and writes
`codex-runs/pee/PREPARED.json`. Refuses while the Claude quota is spent (same reading as
the waker) or the trusted guard is not `ok`.

## Master waker — `ruti operator wake`

Before the master ends a turn for a pause (quota, «Пауза», waiting on Codex), it writes
its checkpoint and records how to come back:

```bash
ruti operator resume-at --after quota --checkpoint .ruti/.../MASTER_RESUME.md --reason "weekly 96%"
ruti operator resume-at --after +90m --checkpoint ... --reason "waiting for CI"
ruti operator resume-at --manual --reason "founder decision needed"   # nobody wakes it
ruti operator resume-at --cancel
```

`--after quota` means the reset of a Claude window that is at its limit (5h ≥ 95%,
week ≥ 97%), or now. The request is `$STATE_ROOT/operator/resume.json`. The waker
checks it every minute and, once due:

* postpones it to the reset while the Claude quota still shows a spent window. It reads
  the freshest of WSL's and Windows' `quota.json` (`/mnt/c/Users/*/AppData/Local/ruti/`,
  or `RUTI_QUOTA_SOURCES`): the status line writes only where a Claude session runs, and
  all sessions spend one subscription. A reading older than 30 minutes uses stricter
  limits (5h ≥ 80%, week ≥ 90%) because it may have grown since;
* does nothing if the master harness is already working;
* if the master is idle for less than 50 minutes (prompt cache still warm) and has one
  tmux pane, pastes a fixed "pause is over, continue from <checkpoint>" message and
  requires the harness to start working within a minute;
* otherwise starts a **new** master in the same folder (`harness new claude <folder>
  --model opus --mode auto --task "read <checkpoint>…"`) and, once `harness ps` lists
  it, closes the old idle one (its conversation is kept). Resuming a large session
  after an hour re-reads all of it uncached — measured at ~90% of a five-hour window.

Three failures turn the request `failed` and add it to `needs_admin.jsonl`. Log:
`waker.jsonl`. Unit: `ops/systemd/ruti-operator-waker.service`.

`harness tell` would be the cleaner way to poke a live master, but it is refused for
programs until the person turns on `harness settings programs-reply`.

Replaces `.ruti/admin-20261109/autoapprove.py` in the HACCP checkout (stopped
2026-11-15): that script had no PID/ownership check, allowed `deno test --allow-all`,
`psql -f /tmp/*.sql` and free-form SELECTs on the shared `postgres` database, ran under
bare `nohup`, and kept no machine-readable log.

## Worktree cleanup (`ruti operator gc`)

`ruti operator gc [--keep haccp-master] [--apply] [--json]` lists linked worktrees under
`.ruti/` and why each stays; `--apply` removes only clean ones, not used by a live
Harness session, whose HEAD is in `origin/main`, is the head of a merged/closed PR, or is
on a remote branch. `git worktree remove` without `--force`; branches are kept.

Under WSL it drives Windows `git.exe`. Worktrees made by Windows git keep `C:/...` paths
in `.git/worktrees/*/gitdir`; WSL git cannot see them and lists **every** such worktree
as `prunable`, so `git worktree prune` from WSL wipes all their metadata. Never run
`git worktree prune` from WSL in this repo; `gc` never runs it at all.

## Folder trust (`ruti operator trust`)

The person trusts every folder on this machine (2026-11-16). Claude Code otherwise stops
a new session on "Is this a project you created or one you trust?" -- and looks for trust
only up to the folder's git root, so a trusted repo does not cover its worktrees.
`ruti operator trust [FOLDER...]` marks folders trusted in `~/.claude.json` (and, under
WSL, in the Windows `~/.claude.json` with `C:/` keys); with no folders it sweeps every
repository under `/mnt/c/mycode`, its `.ruti/*/worktrees/*` and `~/harnesses/*`. The
waker sweeps every minute and trusts the master folder right before `harness new`;
`prepare` trusts the worktree it creates. Codex keeps its own trust in
`~/.codex/config.toml` and is not touched here.
