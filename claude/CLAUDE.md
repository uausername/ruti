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

That size test is the *only* reason to skip `route`, and it is rechecked per task, not
once per session. Already holding context on this codebase, or being several tasks
into a queue of similarly-shaped work, is not a reason to skip it — task five gets the
same check as task one; momentum is not judgement. If several tasks have gone by with
no `route` call, that gap is itself the signal to stop and call it before the next one.

#### Letting the classifier do the classifying

`--describe "<what the task is>"` hands the description to a decision model instead of
taking your `--kind` on faith. It answers in about a second and a half and costs about
$0.000025, and it prints what it decided and what it declined:

```
ruti route --describe "wrap the RLS helpers so Postgres runs them once per query" --files 3 --loc 120 --json
```

It may only make routing **more** cautious. A harder `--kind`, a higher difficulty or
"not trivial after all" are taken on ordinary confidence; anything that would relax
routing needs high confidence before it is accepted, and the output says so either
way. So `--describe` is never a reason to leave `--kind` off — pass your own reading
and let the two disagree in the open.

Do not read it as an oracle. It classifies from the words you give it, so it does not
know that a change to an RLS helper is security work in this codebase unless you say
so; measured on a real node it called exactly that `refactor`. `ruti mode jev off`
turns it off for the session, and `ruti classify "<description>"` asks it without
ranking anyone.

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

It also checks which model really answered — before the run with a probe, and after
it from the proxy's own log of every request. If it reports `substituted: true`, some
of the run's requests were served by a different model group than the alias (the
`usage.note` says how many and which): the result is that model's work, not the
alias's, so judge the alias by it only after rerunning. The proxy has no default
fallback any more, so an alias that is rate-limited or down fails the run with its own
error instead of quietly borrowing Gemini; a failure like that is a reason to pick
another executor, and `ruti doctor --fix` if it repeats.

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

### Wait mode

`ruti mode wait on` keeps a long task from running into the hard five-hour stop. At
90% a hook tells you, once per window, to assess the open tasks: finish only what fits
before 95%, start nothing large, keep the task list current. At 95% every tool call
except `ruti` and TodoWrite is refused -- write a checkpoint in your reply (done, in
progress and where it stopped, remaining steps in order) and end the turn. The `Stop`
hook then waits out the reset and resumes the session with an instruction to continue
from that checkpoint. `ruti mode wait off` releases a pause.

### Default modes

`ruti defaults set coding=on free=soft wait=on flow=on` sets the modes every session
has unless it sets its own; `ruti defaults` shows them, `ruti defaults clear [mode]`
goes back to the built-in ones. A session's own `ruti mode ...` always wins. The status
line and the prompt hook show the effective modes, so trust them over what you
remember setting.

### Flow mode

`ruti mode flow on` hands a long task to a fresh session instead of letting this one's
context fill. At 50% context a hook tells you once: finish the step in hand, then pipe
a handoff into `ruti flow handoff` (heredoc: `ruti flow handoff <<'EOF'` ... `EOF`) --
Goal; Done; In progress, exactly where you stopped; Next steps in order; Key files,
commands and state; Decisions and constraints; Instructions to your next self -- and
end the turn. Write it for a reader with none of this conversation: that is who reads
it. From 60% without a handoff, and always once one is written, every tool except
`ruti` and TodoWrite is refused. The `Stop` hook then opens `claude` in a new window in
the same directory and permission mode; that session starts with the handoff and this
session's modes in its context. At most five sessions in a chain. `ruti mode flow off`
drops a handoff not yet launched. With flow on, this replaces the 50% advice in
"Context window watch" -- hand off instead of offering `/compact`.

### Standing a council

`ruti mode council on|auto` turns the one-off `ruti council` command into a standing
policy for the session, and shows it in the status line (`council` / `council?`) so an
expensive mode is never on without being visible.

* `on` — convene before genuinely ambiguous, hard-to-reverse calls: an architecture
  choice, a product judgement, a tradeoff with no obviously right side. Not for
  mechanical work; `route` is for that.
* `auto` — hand the question over and let it decide. It convenes only when the call is
  *both* ambiguous and expensive to get wrong, and declines everything else for about
  $0.000025, so a question that did not need a council costs one small call instead of
  N paid ones. "What should we name the flag?" is ambiguous and still gets refused.

Either level also adds a judge to `ruti council`: it names the answer that argues its
case best and says whether the council agreed at all. Read it as a pointer, not a
verdict — it ranks how well an answer is argued, not whether it is true, and the raw
answers are always printed in full for exactly that reason. A council whose opinions
you did not read is a council you paid for and threw away.

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

A local model being down or unloaded is not a reason to skip delegation, or to fall
back to doing the work in-session — it means only that a `local-*` alias specifically
is not ready. Remote executors need internet, not a working local model. `ruti doctor`
flagging `lmstudio` or `local-route`, or a `local-*` delegation failing, means "this
local alias isn't ready — delegate elsewhere, or run `ruti doctor --fix`",
not "delegation is blocked." Local models exist for the case of no internet connection
on this machine; do not generalise that into "the local model must be fixed before
delegating."

### When something is wrong

`ruti doctor` checks the failures that are otherwise silent — intercepted TLS, a
stopped LM Studio server, a model list that drifted from disk, a proxy listening on
every interface. `ruti doctor --fix` repairs what it safely can. If a delegation fails
twice for the same reason, do the work yourself rather than retrying: a retry loop
costs more turns than the delegation would have saved.
