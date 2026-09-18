# Append to ~/.claude/CLAUDE.md, or install it with `ruti install --claude-md`.
# Without this Claude Code has no reason to prefer a delegate over doing the work itself.

## Routing implementation work

This machine runs `ruti`, which knows what models are available locally and
remotely, how much of the Claude subscription window is left, and what each option
costs. Claude Code stays exactly as it is — same subscription, same session — and
gains a way to hand off work that does not need its judgement. Day-to-day use
(`route`/`delegate`/`doctor`) never needs its source location; the command is on
PATH regardless of clone path, and that path is not the same on every machine. Only
a task that edits `ruti` itself needs the source, and how it was installed varies
by machine (pip, pipx, uv, conda, ...) — trace the command instead of assuming a
package manager: `where ruti` (or `which ruti`) finds the entry point, and `pip
show ruti` adds the `Editable project location` line when pip put it there.

**The manager must not run out of budget.** If this session hits the five-hour limit,
nothing else gets assigned any work either. There is no paid overage on this account,
so the limit is a hard stop. Protecting the manager's remaining budget takes priority
over finishing any individual task quickly.

### First step of any substantial task

Classify the task, then ask for a ranking. Classification is yours; the facts are not.

```
ruti route --kind implement --files 12 --loc 800 --repo-context large --json
```

`--kind` is one of `boilerplate`, `implement`, `refactor`, `debug`, `analyze`,
`review`, `security`. The output ranks eligible executors, states why each was ruled
out, and gives the exact command to run. A ranking is recorded as advice, and the
prompt hook reminds you while it goes unfollowed; add `--probe` when you only want to
see what route would say, not start a task.

Skip the call when the task is obviously small — under ~40 lines in one or two files.
Routing that costs a tool call to be told "just write it" is itself a waste.

### Delegating

Never call `opencode` directly. Use:

```
ruti delegate --model <alias> --dir <path> --task "<complete brief>" --json
```

It runs the delegate under a timeout, keeps the verbose output in a log file, and
returns a short summary — exit code, files changed, `git diff --stat`. That containment
is where the saving actually comes from: **the subscription burn is driven by context
length**, so the win is not that the delegate is cheaper, it is that its output never
enters this window.

It also checks which model really answered. If it reports `substituted: true`, a
backend is down and LiteLLM's fallback took the request — work meant to stay on this
machine went to a remote provider instead. Stop and run `ruti doctor --fix`.

`model_effective` is the model that actually did the work. For a router alias such as
`pareto-code` it is the router's own pick, which can be a frontier model billed in USD
— "no subscription quota" is not "free". `unknown` means the proxy could not say; the
`usage.note` field says why. Read `route`'s `metered` / `pays_in` before picking a
metered executor for work a free one could do.

For a large delegation, hand the run to the `delegate-runner` agent instead, and the
resulting diff to `delegate-verifier` when it exceeds ~3 files or ~200 lines. Below
that, read the diff yourself; two subagents for a small change cost more than they save.

### Turning delegation off for one session

`ruti off` disables `route` and `delegate` — both refuse until re-enabled — for the
current Claude Code session only; a different session, or this one after it ends, is
unaffected. `ruti on` re-enables them. Nothing here is written to CLAUDE.md: the
toggle lives in `ruti`'s own state, keyed to the session, so it cannot leak into a
future session or another project. Use it when the user says to stop delegating for
now, or when a task's judgement calls are dense enough that routing overhead is not
worth it — `ruti status` shows whether it is currently off.

### Coding mode and free mode

Two session-scoped modes, set the same way `ruti off` is and shown in the status line
(`code`, `free`):

* `ruti mode coding on` — a hint, not a gate. While it is on, `route` ranks
  coding-tuned aliases up and the prompt hook names the registered ones to reach for
  when you delegate — only the zero-cost ones under `free hard`, which refuses
  `pareto-code`. Turn it on when the session's work is programming; leave it off for
  everything else ruti is used for.
* `ruti mode free soft|hard` — keep delegation on zero-cost models. `soft`
  deprioritises paid metered APIs in `ruti route` and warns before `ruti delegate`
  uses one; `hard` makes `route` rule them out and `delegate` refuse them outright.
  The Claude subscription, local models and Anthropic subagents are all money-free
  and unaffected — this is only about paid provider keys.

`ruti openrouter models` lists the recommended coding models (ruti's shortlist plus
OpenRouter's live catalogue); `ruti openrouter setup` registers the routers
(`pareto-code`, `free`) and any free models you pick as routable aliases. Neither the
modes nor the registration touch this file.

### What stays in this session

Planning and architecture. Anything needing back-and-forth exploration of the codebase
first. Security-sensitive code. Final review of anything a delegate produced. `ruti
route` already refuses to delegate `security` and `review` work — that is policy, not
a scoring accident.

### Budget bands

A hook injects the current band on each prompt, so act on what it says rather than
guessing. In outline: **GREEN** — delegate bulk generation freely. **YELLOW** —
delegate aggressively, drop effort for planning. **ORANGE** — no Opus, everything
mechanical goes to a non-Anthropic executor, no speculative repo exploration.
**RED** — finish what is open, write a handoff, warn the user. **CRITICAL** — stop.
**UNKNOWN** — the reading is stale; assume the window is mostly spent.

Anthropic subagents are legitimate executors, but they draw on the *same* window. A
Haiku subagent is worth spawning because its context stays out of this one, not
because its tokens are free.

### Local models

`ruti model use <key>` loads a model, deciding for itself whether to load alongside or
evict, sizing the context to fit the GPU, and measuring the result. Do not pass a
context length unless there is a reason to: asking for more than fits does not fail,
it spills into system memory and runs an order of magnitude slower.

Two facts worth carrying: `opencode` sends an **8095-token system prompt** before any
task text, so a model with a smaller window cannot run it at all — it fails, and on
the way it may claim to have written files it never touched. And a model that cannot
emit structured tool calls cannot drive `opencode` regardless of how good it is at
code; `ruti route` gates on both.

### When something is wrong

`ruti doctor` checks the failures that are otherwise silent — intercepted TLS, a
stopped LM Studio server, a model list that drifted from disk, a proxy listening on
every interface. `ruti doctor --fix` repairs what it safely can. If a delegation fails
twice for the same reason, do the work yourself rather than retrying: a retry loop
costs more turns than the delegation would have saved.
