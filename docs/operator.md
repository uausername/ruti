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
