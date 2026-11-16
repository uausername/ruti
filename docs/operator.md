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

Replaces `.ruti/admin-20261109/autoapprove.py` in the HACCP checkout (stopped
2026-11-15): that script had no PID/ownership check, allowed `deno test --allow-all`,
`psql -f /tmp/*.sql` and free-form SELECTs on the shared `postgres` database, ran under
bare `nohup`, and kept no machine-readable log.
