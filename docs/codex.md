# Using ruti from Codex

Codex can plan and review while ruti delegates a bounded implementation task to
OpenCode. Existing Claude integration remains the default.

```powershell
# Preview; --apply installs a managed section, preserving a backup and user text.
ruti install --host codex
ruti install --host codex --apply

$env:RUTI_HOST = 'codex'
$env:RUTI_SESSION_ID = 'one-stable-id-for-this-conversation'
ruti status --json
ruti delegate --model gemini-flash --task-file task.md --dir C:\project --timeout 900 --json
# Review the diff and run relevant tests, then use the returned run ID:
ruti verdict ok --run RUN_ID
ruti report --host codex --json
```

`CODEX_THREAD_ID` is used when available and `RUTI_SESSION_ID` is absent. Codex
session IDs are namespaced. Claude quota is never used as Codex subscription usage.
Codex history defaults to Codex only; `report --host all` includes all managers.
Verdicts determine accepted work; process success alone is weaker evidence.
The report counts lines in changed files, not a measured token or subscription saving.

## Isolated implementation

```powershell
ruti delegate --model gemini-flash --task-file task.md --dir C:\project --worktree --json
# Inspect the returned worktree directory, run its tests and review the patch.
ruti worktree apply --run RUN_ID --dir C:\project --json
ruti verdict ok --run RUN_ID
```

`--dir` must be the Git repository root with an existing HEAD. The new detached
checkout starts at that commit: staged, unstaged and untracked source edits are
preserved in the original and excluded from the delegate's checkout. Use the
ordinary mode when the task must build on those uncommitted edits.

The result includes a worktree directory and an exported binary-capable patch.
The original branch, working files and index are not changed by delegation.
Import checks the source path, unchanged HEAD, patch hash and applicability before
applying. It leaves the original index unchanged and refuses conflicting or repeated
application. No automatic stash, reset, merge, commit or deletion is performed.
Partial results after a timeout are also exported for review.

Worktree mode pins OpenCode's build agent and denies external-directory access.
This helps prevent accidental writes to sibling checkouts; it is not an OS security
sandbox. Worktrees share Git objects, and the caller must still enforce runner
permissions. An empty patch is not an accepted implementation merely because the
process exited zero. Check the actual files and tests before giving a verdict.

Worktrees and patch artifacts remain for inspection. Removal and automatic garbage
collection are not implemented. Reproducible live check on a synthetic repository:

```powershell
py -3.12 scripts/smoke_worktree_delegation.py --model ruti-router/gemini-flash
```

The design uses Git's documented [detached worktrees](https://git-scm.com/docs/git-worktree),
[binary patch output](https://git-scm.com/docs/git-diff),
[patch checks and default atomic application](https://git-scm.com/docs/git-apply),
and OpenCode's [external-directory permissions](https://docs.opencode.ai/docs/permissions/).

## Local model routing

```powershell
ruti delegate --model local-qwen3-4b --local-only --task-file task.md --dir C:\project --json
```

`--local-only` bypasses LiteLLM and connects model calls directly to LM Studio on
loopback. It configures a dedicated provider, pins the main and small model, disables
other providers and automatic sharing/updates, and uses OpenCode's `--pure` mode.
Loading failure stops the run before the brief is sent. This is model routing,
not a network sandbox for shell tools or MCP servers.

The local context must hold OpenCode's system/tool prompt plus task and response.
OpenCode 1.18.13 measured 17,447 input tokens on the live smoke test. The earlier
8,095-token estimate was stale; the planner now budgets 18,000 plus brief and
response headroom. On the tested 6 GB GPU, the downloaded Qwen3-4B could not fit
the required context under the GPU-only planner policy. The command refused
instead of switching to Gemini. CPU offload and new downloads are not automatic.

## Parallel runs and attribution

Use separate checkouts or worktrees, and do not modify a running delegate's checkout.
Each run has separate task/log filenames and a running marker. One finishing run
cannot remove another's marker. Normal LiteLLM requests carry `x-ruti-run-id`;
the updated proxy callback records it without logging authentication headers.
Restart the proxy once after upgrading the callback. Records without a matching
run ID stay unattributed rather than being assigned to a concurrent job.

Run an explicit live check, which sends tiny file-creation briefs from temporary
Git directories to the chosen provider:

```powershell
py -3.12 scripts/smoke_parallel_delegation.py --model ruti-router/gemini-flash
```

Two simultaneous Gemini runs were verified with independently attributed request
counts of four and three. No monetary price was stated; it remains unknown.
This checks correctness, not representative savings on real implementation tasks.

The runtime configuration uses OpenCode's documented
[inline configuration and provider allowlist](https://docs.opencode.ai/docs/config/)
and [custom provider headers](https://docs.opencode.ai/docs/providers/).
