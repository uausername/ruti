## Delegate implementation with ruti

Keep architecture, security-sensitive decisions and final review in Codex. Delegate
substantial, well-scoped implementation tasks through the existing ruti CLI.

For every ruti invocation, set `RUTI_HOST=codex` and `RUTI_SESSION_ID` to one stable
identifier for this conversation. Reuse `CODEX_THREAD_ID` if available; otherwise
generate an identifier once and retain it across commands. Never use Claude quota
as a measurement of Codex subscription usage.

Check `ruti status --json`. Choose an available alias; do not assume
`lm-studio-local` exists. LM Studio may be stopped. If a backend fails, avoid
repeated retries; continue in Codex or use an explicitly permitted alternative.

Write a short task brief to a file, specifying scope, allowed files and validation.
Run `ruti delegate --model ALIAS --task-file TASK.md --dir PROJECT --timeout 900 --json`.
For local model routing without remote fallback, add `--local-only` and use a
`local-*` alias. This connects directly to LM Studio and fails if loading fails.
It does not restrict network access by tools or MCP servers.
Use background shell execution and poll for long tasks. Read the compact result,
then review the actual diff and run relevant checks. Exit zero is not acceptance.
Record `ruti verdict ok|bad --run RUN_ID` after review. After a timeout inspect the
partial changes before retrying. Preserve pre-existing edits; use a worktree for
substantial changes when appropriate. Do not delegate overlapping file edits.

All runner actions remain subject to the current workspace, network and approval
permissions. Never use delegation to bypass a restriction. Use local-only work only
when the route actually enforces it; a probe alone does not establish this.
Do not install Claude hooks, switch Claude seats, or infer Codex quota percentages.
Parallel delegates must use separate checkouts/worktrees. Do not edit a delegate's
checkout while it runs: changed-file snapshots cannot distinguish those edits.
For tasks that can start from the last commit, add `--worktree`. The isolated
checkout excludes uncommitted source edits. Review and test its result, then use
`ruti worktree apply --run RUN_ID --dir ORIGINAL_PROJECT` to import the patch.
The source index stays unchanged; conflicts or changed HEAD require manual review.
Use `ruti report --host codex --json` for reviewed outcomes and known costs;
subscription savings remain unknown without telemetry.
