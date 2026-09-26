# ruti

[![CI](https://github.com/uausername/ruti/actions/workflows/ci.yml/badge.svg)](https://github.com/uausername/ruti/actions/workflows/ci.yml)

**Stop burning your Claude Code subscription on boilerplate.**

`ruti` keeps Claude Code as your architect and reviewer, and routes the token-heavy
typing to a model on your own GPU, a free API tier, or a cheap one — *without leaving
your session*.

No proxy in front of Claude. No re-authentication. No lost context.

---

## The problem

You're on a Claude Code subscription with a rolling five-hour limit. You hit it, and
now you wait. Meanwhile you have a GPU sitting idle and free API tiers going unused.

The obvious fix — point `ANTHROPIC_BASE_URL` at a router that falls back to a local
model — **does not work**, for two independent reasons:

1. **Technically:** setting `ANTHROPIC_BASE_URL` disables Claude Code's OAuth entirely.
   Your subscription stops applying; the CLI demands a pay-per-token API key instead.
   Auth is read once at process start and cannot be swapped at runtime.
2. **Contractually:** Anthropic's terms prohibit routing Free/Pro/Max subscription
   credentials through third-party tools.

Hooks don't rescue it either. `StopFailure` fires *after* a rate-limit error and can
only log or notify — it cannot intercept the turn or substitute another model's answer.

So "seamlessly continue this conversation on a different backend" is architecturally
impossible.

## The insight

Don't switch Claude's backend. **Give Claude a tool that is another coding agent**, and
the information it needs to decide when using it is worth the trouble.

Claude Code stays exactly as it is — same OAuth, same subscription, same unbroken
session. It gains a CLI that knows what models exist, what fits in the GPU, what has
been verified to work, and how much of the five-hour window is left. Because that's an
ordinary Bash call, nothing about your session changes.

The saving is not that a delegate's tokens are cheaper. **It's that the delegate's
output never enters Claude's context** — and context length is what drives the burn
rate. `ruti delegate` runs the delegate, keeps its tens of thousands of tokens of tool
traffic in a log file, and hands back six lines.

## Architecture

```
Claude Code  (subscription, OAuth, untouched)
     │
     ├── ruti route      → which executor, given the task, the hardware, and the budget
     ├── ruti delegate   → runs opencode, returns a summary instead of a transcript
     ├── ruti model use  → loads/evicts local models, sized to actually fit the GPU
     ├── ruti provider   → adds API providers, testing the key before writing anything
     ├── ruti openrouter → the pareto-code / free routers and free coding models, from the live catalogue
     ├── ruti mode       → coding / free toggles for the session, shown in the status line
     ├── ruti report     → whether any of this is actually paying for itself
     └── ruti doctor     → finds the failures that are otherwise silent
                    │
                    ▼
            LiteLLM proxy  :4000        ← loopback only, auto-starts at logon
                    ├──► LM Studio :1234   → local models   (free, private)
                    ├──► OpenRouter        → pareto-code, free router, hundreds of :free models
                    └──► Gemini / any provider you add      (free tiers, cheap tiers)
```

A `statusLine` script captures the subscription's remaining budget — the only place
Claude Code exposes it locally — and a `UserPromptSubmit` hook feeds it back so the
manager is always aware of its own budget without spending a tool call to ask.

## What you get

| | |
|---|---|
| **Your session is never interrupted** | Claude Code runs unmodified — no env vars, no proxy in front of it |
| **Local work is free and private** | Code never leaves the machine when routed to LM Studio |
| **Model swaps are automatic** | `ruti model use` decides load-alongside vs evict, sizes the context to the GPU, and measures the result |
| **The GPU comes back** | A loaded model releases its VRAM after 15 minutes idle, so it never quietly blocks a game; `ruti model unload` frees it now |
| **Keys are tested before they're saved** | Four stages with *distinguishable* failures — a broken certificate chain never reads as a bad key |
| **Budget-aware routing** | Five bands from GREEN to CRITICAL, driven by burn rate as well as level |
| **Silent failures made loud** | A "local" request answered by a remote fallback is reported, not ignored |
| **A session-scoped kill switch** | `ruti off`/`ruti on` disable and re-enable `route`/`delegate` for the current Claude Code session only — nothing persists to another session or project |
| **A coding mode** | `ruti mode coding on` tells the manager, on every prompt, to reach for OpenRouter's `pareto-code` router and coding-tuned models when it delegates — because `ruti` is used for more than code, and the hint only makes sense while you are writing some |
| **A free mode, soft or hard** | `ruti mode free soft` deprioritises paid metered APIs and warns before one is used; `ruti mode free hard` rules them out entirely. The subscription, local models and Anthropic subagents are money-free and unaffected |
| **A wait mode** | `ruti mode wait on` rides the five-hour window to its edge instead of off it: at 90% the manager is told to assess its open tasks, at 95% tool calls are refused and it writes a checkpoint, and after the reset the `Stop` hook resumes the same session by itself. Shown as `wait` / `paused->HH:MM` |
| **Default modes** | `ruti defaults set coding=on free=soft wait=on` gives every session those modes unless it sets its own with `ruti mode ...`; `ruti defaults` shows them, `ruti defaults clear` restores the built-in ones |
| **A council mode** | `ruti mode council auto` lets a typed decision model judge, per question, whether a call is ambiguous *and* expensive enough to be worth several paid answers — and refuse when it is not. Visible in the status line the whole time it is on |
| **OpenRouter coding models, one command** | `ruti openrouter models` merges a vetted shortlist with OpenRouter's live catalogue; `ruti openrouter setup` registers the `pareto-code` / `free` routers and any `:free` models you pick as routable aliases |

## Command reference

Every command that matters, what it actually does, and why it exists. `-h`/`--help`
on any of them gives the same detail from the CLI itself; `--json` on the read
commands makes the output machine-parseable for scripting.

### Deciding and delegating — the core loop

| Command | What it does | Why it earns a place in your workflow |
|---|---|---|
| `ruti route --kind K --files N --loc N [--describe "..."] [--repo-context ...] [--risk ...] [--json]` | Ranks every eligible executor (local GPU, remote API, Anthropic subagent, or "write it yourself") for a task you've already classified — scored on budget, speed, and capability, with each rejected candidate showing *why* it was ruled out. Every executor is tagged with how it is paid for — subscription window, nothing, or metered USD — and routers are marked as such, since their model and price are chosen per request. On an easy task (difficulty ≤ 0.5) a metered executor ranks below every free one, coding mode or not. | This is the one call that turns "should I delegate this?" from a guess into a decision backed by live facts: what's actually loaded, what actually fits, what the subscription actually has left. Costs one cheap tool call to avoid a wrong, expensive one. `--describe` adds the classifier below, so the `--kind` it ranks on stops being a guess. |
| `ruti classify "<description>" [--transport ...] [--json]` | Asks TypeSafe's Jev decision model what a task actually is — its kind, how much judgement it demands, and whether it is small enough to skip routing altogether — each answer carrying a calibrated confidence. About 1.5s and $0.000025 a call, over the OpenRouter key you already have. | The one judgement in this tool that was never code: `--kind` was whatever the manager asserted, and the documented failure is that it stops asserting anything once a session finds a rhythm. Wired into `route --describe` it may only ever make routing *more* cautious — relaxing it needs high confidence — so a wrong guess costs an unnecessary delegation, never a security task handed to a cheap model. |
| `ruti delegate --model M (--task "..." \| --task-file F) [--dir D] [--json]` | Runs the task through `opencode` against the chosen model, under a timeout, with verbose tool traffic captured to a log file — and hands back only a short summary (exit code, files changed, `git diff --stat`). Refuses paid models outright if free mode is `hard`, and flags it if the answering model isn't the one you asked for (`substituted: true` — a backend is down and LiteLLM silently failed over). Separately reports the model that really did the work (`model_effective`, printed as `pareto-code -> anthropic/...`) and, where the provider states it, what it cost — read from a proxy callback (`litellm/ruti_usage.py`), because the proxy rewrites every response to carry the alias. | **This is where the actual saving lives.** Not cheaper tokens — tokens that never enter Claude's context at all. A delegate can produce tens of thousands of tokens of tool chatter; you only ever see six lines of it. |
| `ruti council "question" [--models ...] [--auto] [--judge] [--timeout ...] [--yes] [--json]` | Fires the same question at several models in parallel and prints every raw answer, unreconciled. `--auto` checks first whether the question is ambiguous *and* expensive to get wrong, and declines cheaply when it is not. `--judge` adds a typed verdict: which answer argues its case best, and whether the council agreed at all. | For the genuinely hard or ambiguous call, not mechanical work. Spends real money and context on purpose, because for that one class of question a second (and third) independent opinion is worth more than a single fast answer. The judge costs $0.000025 and never replaces the answers — they are always printed in full, because a council hidden behind a verdict would have no reason to exist. |

### Session controls — scoped to *this* Claude Code session only

| Command | What it does | Why it earns a place in your workflow |
|---|---|---|
| `ruti off` / `ruti on` | Disables/re-enables `route` and `delegate` for the current session's `CLAUDE_CODE_SESSION_ID` — nothing else. | A kill switch that can't leak into your next project by accident. Useful the moment you want Claude to just write the code itself for a while, without deregistering anything permanent. |
| `ruti mode coding {on\|off}` | Turns on a prompt-hook hint telling the manager to prefer OpenRouter's `pareto-code` router and other coding-tuned models when it delegates. | `ruti` isn't only for programming sessions, so this hint should only fire while you're actually writing code — and it should survive this session's own context compaction, which a purely verbal instruction to Claude cannot. |
| `ruti mode free {off\|soft\|hard}` | `soft` flags and deprioritises paid metered APIs in `route`'s ranking; `hard` makes `route` rule them out entirely and `delegate` refuse to run them. Local models, the Anthropic subscription, and self are always money-free and untouched either way. | Lets you decide, per session, whether "cheap" is good enough or you want a hard guarantee that nothing billed gets touched — without editing any config. |
| `ruti defaults [set KEY=VALUE...\|clear [KEY...]]` | Shows or changes the modes a session has when it has not set them itself. A session's own `ruti mode ...` always wins. | Modes are per session on purpose, but most sessions on a machine want the same ones — this saves setting them by hand every time without making any of them global. |
| `ruti mode wait {on\|off}` | At 90% of the five-hour window a `PostToolUse` hook asks the manager, once per window, to assess the open work; at 95% a `PreToolUse` hook refuses every tool but `ruti` and TodoWrite and asks for a checkpoint; the `Stop` hook then sleeps until `resets_at` (plus 90 s) and answers `block`, so the same interactive session carries on. Esc cancels the wait. | There is no paid overage: hitting 100% mid-task loses the turn. Trading the last 5% for a clean, resumable break is cheaper than that, and the reset time is used rather than watching the number fall, because nothing refreshes the reading while the session sits idle. |
| `ruti mode council {off\|on\|auto}` | `on` tells the manager to stand a council before ambiguous, hard-to-reverse calls; `auto` lets the classifier decide per question and refuse most of them. Either way `ruti council` gains a judge. Shown in the status line as `council` / `council?`. | Every other mode here saves money or context; this is the one that spends more, deliberately. So it defaults to off, it is visible at a glance while it is on, and `auto` has to justify each convening before N paid calls happen. |
| `ruti mode jev {on\|off}` | Turns the task classifier off for this session. On by default wherever a key exists. | It sends the task description to a third party, and some sessions should not. Off, `route` simply takes your flags as given, exactly as it did before. |

### Setup and inventory — run once, or whenever the picture changes

| Command | What it does | Why it earns a place in your workflow |
|---|---|---|
| `ruti install [--apply]` | Wires `ruti` into Claude Code: status line, hooks, agents, and the policy file. Previews the diff by default; `--apply` writes it and keeps backups. | The entire point is that Claude Code itself stays untouched in every way that matters (OAuth, subscription, session) — this command makes the *few* things that do need wiring reviewable before they're written. |
| `ruti model use KEY [-c N] [--min-context N] [--ttl N] [--dry-run]` | Makes a local model resident: decides for itself whether to load alongside what's already loaded or evict it, sizes the context window to what the GPU can actually hold, and measures the real result rather than trusting the loader's own estimate. | Model memory math is fiddly and silently wrong if done by hand (`lms load --estimate-only` is a stub that ignores context size entirely). This is a decision engine, not a thin wrapper. |
| `ruti model unload [IDENTIFIER] [--all]` | Frees a loaded model's VRAM immediately. | Loaded models already auto-release after 15 minutes idle so a forgotten model never quietly blocks a game — this is for when you want the GPU back *now*. |
| `ruti models [--json]` | Lists every local model and the largest context each one actually fits at, given current GPU memory. | The fact that decides whether a model can run `opencode` at all — a context window too small doesn't run slow, it fails outright. |
| `ruti models sync` | Regenerates the LiteLLM model list from what's actually on disk. | Keeps the proxy's model list truthful after you download or remove something in LM Studio, without a manual config edit. |
| `ruti provider add [--provider ...] [--model ...] [--alias ...] [--api-base ...] [--key-stdin] [--yes]` | Adds a remote API provider — but writes nothing until the key is tested against four distinguishable failure stages (bad cert chain, bad key, no chat support, no tool-call support). | A key that "doesn't work" is not one problem, it's four different ones, and conflating them wastes time. TLS interception from antivirus software looks *exactly* like a bad key unless you separate the checks. |
| `ruti provider list [--json]` / `ruti provider test [ALIAS] [--json]` / `ruti provider remove ALIAS [--yes]` | Show what's registered, re-run the key checks for one or all providers, or remove a provider (the key itself stays in `.env` unless you say otherwise). | Providers drift — keys expire, tiers change. These give you a live read instead of trusting a config file you wrote weeks ago. |
| `ruti openrouter models [--free/--all] [--coding/--any] [--refresh] [--json]` | Shows the recommended coding models: a hand-checked shortlist merged live against OpenRouter's own `/api/v1/models` catalogue, defaulting to free, tool-capable models only. | A pasted "top free coding models" list off the internet is half real at best — this checks every slug against the live catalogue so a model that's vanished shows as `missing` instead of 404ing mid-delegation. |
| `ruti openrouter setup [--models ...] [--key-stdin] [--skip-verify] [--yes]` | Registers the `pareto-code` and `free` OpenRouter routers, plus any `:free` models you pick, as routable proxy aliases — after testing the key for chat and tool-call support. | The "coding harness" switch on the plumbing side. Without it, `coding` mode has nothing coding-tuned to point at, and `free hard` mode has no zero-cost remote option to fall back to. |

### Visibility — know what's actually happening before you trust it

| Command | What it does | Why it earns a place in your workflow |
|---|---|---|
| `ruti status [--json]` | One screen: subscription budget and band, GPU memory, which local models are loaded, proxy health. | The fastest way to answer "can I delegate right now, and to what" without piecing it together from four other commands. |
| `ruti doctor [--fix]` | Checks the failures that would otherwise stay completely silent: TLS interception, a stopped LM Studio server, a model list that's drifted from disk, a proxy bound to every network interface or running with administrator rights it does not need, a "local" request quietly answered by a remote fallback. `--fix` repairs what it safely can. | Every one of these failure modes was discovered the hard way, because none of them throw an error — they just quietly produce a worse answer from a different place than you asked. This is the single command that surfaces all of them at once. |
| `ruti report [--days N] [--json]` | Shows what delegation has actually bought you — lines written by delegates and their approximate token cost, how much delegate transcript stayed in logs instead of your context, and money spent per provider and per model that actually answered (only prices the provider itself stated; anything else is labelled as not counted) — from a ledger `ruti` keeps as it goes. | Built to be falsifiable on purpose: it already caught its own bad assumptions twice (verbose output turned out to be terse; "successful" delegations turned out to include code that never should have been counted). It only claims what it can actually measure. |

### Status line and prompt hook — always on, nothing to run

These aren't commands you type — `ruti install --apply` wires them in, and from then
on they run automatically on every turn:

| Piece | What it shows | Why it earns a place in your workflow |
|---|---|---|
| Status line | Subscription budget and band (GREEN→CRITICAL), marked `~stale`/`~unknown` when the reading is not live; 7-day usage, coloured to the band the moment it is the reason the band is that tight rather than the five-hour window; current model/effort; `OFF` first and loudest if `ruti off` is set; `code` / `free` / `free!` / `council` / `council?` / `wait` / `paused->HH:MM` while those modes are active, and `jev` always — green when the classifier will answer, dim when the mode is off or no key is configured; the session's known Jev spend (`jev:$…`) once it is above zero; and a `doctor:N` badge, amber or red, from whatever the last session-start health check found. | Claude Code exposes the remaining subscription budget in exactly one place locally, and nowhere else. Losing this silently (three separate Windows-specific ways it can go blank) means the router quietly behaves as if the window were nearly spent. Every one of these started as a fact that existed somewhere in `ruti`'s state but had no presence here — `ruti off` in particular used to have none at all, with only a hook message that scrolls out of view within a prompt or two as its only sign. |
| `UserPromptSubmit` hook | Feeds the budget band and its guidance, plus any active session modes, back into context on every prompt. | The manager gets budget-awareness for free, without spending a tool call to ask — and a mode you turned on ten prompts ago doesn't get silently forgotten. |

## Requirements

- **Claude Code** with a subscription
- **LM Studio** ≥ 0.4 + `llmster` for a scriptable `lms` CLI (optional — remote
  providers work without it)
- **Python 3.12**, **Node.js**
- One or more API keys (a Google AI Studio free-tier key is enough to start)

## Setup

```powershell
# 1. The package. Every dependency already ships with litellm.
pip install "litellm[proxy]"
cd C:\mycode\ruti
pip install -e . --no-deps

# 2. Keys
cp litellm\.env.example litellm\.env    # then put your real keys in it

# 2b. OpenRouter coding models (optional) - registers pareto-code, the free
#     router, and the free :free models you pick; needs an OpenRouter key.
ruti openrouter models        # see what is on offer
ruti openrouter setup         # register a set, then restart the proxy

# 3. Local model (optional)
irm https://lmstudio.ai/install.ps1 | iex
lms get qwen/qwen3-4b -y
lms server start
ruti models sync

# 4. Delegate CLI
npm install -g opencode-ai
mkdir "$env:USERPROFILE\.config\opencode"
cp opencode\opencode.json "$env:USERPROFILE\.config\opencode\opencode.json"

# 5. Proxy at logon
# RunLevel Limited: the proxy needs no administrator rights -- :4000 is not a
# privileged port, and litellm/.env only has to be readable by your own account
# (`ruti doctor`'s secrets check makes sure no *other* user can read it). An elevated
# proxy is worse on every count: ruti cannot restart it, the task can hang Queued,
# and it serves unauthenticated local requests as administrator. An older setup
# registered it with Highest; `ruti doctor` prints the admin commands to change it.
# pythonw.exe, not powershell.exe: Windows 11 hands a non-elevated console to Windows
# Terminal, which ignores -WindowStyle Hidden and leaves an empty window whose closing
# stops the proxy. start_litellm.pyw never creates a console at all.
# (litellm\start-litellm.ps1 does the same job in the foreground, for a start by hand.)
$pythonw   = python -c "import sys, pathlib; print(pathlib.Path(sys.executable).with_name('pythonw.exe'))"
$action    = New-ScheduledTaskAction -Execute $pythonw `
  -Argument '"C:\mycode\ruti\litellm\start_litellm.pyw"' -WorkingDirectory "C:\mycode\ruti\litellm"
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
# The defaults would stop the proxy when the laptop is unplugged and after 72 hours.
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "RutiLiteLLM" -Action $action -Trigger $trigger -Principal $principal `
  -Settings $settings -Force
Start-ScheduledTask -TaskName "RutiLiteLLM"

# 6. Wire it into Claude Code (previews the diff; --apply writes it, keeping backups)
ruti install
ruti install --apply

# 7. Check
ruti doctor
```

## Does it actually save anything?

`ruti report` answers that from a ledger it keeps as it goes, rather than asking you to
take the premise on faith.

```
Work the manager did not have to type
  88 lines written by delegates  (~1,056 tokens at 12/line)

Delegate transcript kept in logs
  emitted 2 KB, summarised to 2 KB, 0 KB never read
```

Building this measurement immediately corrected the design. The original claim was that
the saving came from containing `opencode`'s verbose output — measurement showed that
output is terse (five files created produced 559 bytes of stdout), so containing it is
worth very little. The saving that *is* real is the generated code never passing
through the manager's context on its way to disk. The report leads with that number
because it's the one that holds up.

Session-level utilisation is recorded too, but presented as observation rather than
proof: the same window is shared with every other project, and no counterfactual was
ever run.

The first run against an unfamiliar real project corrected it a second time. Two
delegations exited 0 and produced 75 lines that all had to be thrown away — one with a
syntax error, one a `def test_placeholder(): assert True` stub — and both were counted
as work saved. `ruti delegate` now parses any Python a delegate wrote and fails the run
if it does not compile, and `ruti report` counts only successful runs while saying
plainly that "successful" means the process exited cleanly and the code parses, which
is a good deal weaker than the work being usable.

## Choosing a local model

Two hard requirements, and neither correlates with how good the model is at code:

**It must emit structured tool calls.** The delegate has to *call* the write-file tool,
not describe calling it. `lms ls --json` reports `trainedForToolUse`, and `ruti` gates
on it.

**Its context window must hold `opencode`'s prompt.** Measured: `opencode run` sends
**8095 tokens** before any task text. A model with a smaller window does not run
slowly — it fails, and on the way it may report writing files it never touched.

That second constraint is sharper than it looks. On a 6 GB card, Llama-3.1-8B fits only
at ~4096 tokens of context, which is *half* what `opencode` needs before the task even
starts. A 4B model at ~2.3 GB leaves room for a 16k window and works. Bigger is not
better here; `ruti models` shows the largest context each model actually fits at.

## Notes from the build

Things that cost time here, so they don't cost you any:

- **Antivirus TLS interception looks exactly like a bad API key.** Avast's Web Shield
  re-signs every HTTPS connection with a root that is in the Windows store but not in
  `certifi` — which is what LiteLLM verifies against. Every provider call fails with
  `CERTIFICATE_VERIFY_FAILED`. `ruti doctor --fix-tls` builds a merged bundle;
  `ruti provider add` reports it as interception rather than blaming your key.
- **And it breaks git separately.** git does not use `certifi`: Git for Windows ships
  its own `ca-bundle.crt` and defaults to the openssl backend, so `git push` fails with
  `unable to get local issuer certificate` while every ruti check is green. Discovered
  publishing this repository. `ruti doctor` now checks git too, and fixes it by
  verifying against the Windows certificate store rather than by verifying less.
- **The LM Studio GUI being open does not mean its server is running.** They start
  independently, and a stopped server means every "local" request quietly answers from
  a remote fallback instead. That is indistinguishable from success unless you look at
  which model replied — which `ruti delegate` now does.
- **The status line has three Windows traps, and all three fail silently.** It accepts
  only `type`, `command`, `padding`, `refreshInterval` and `hideVimModeIndicator` — an
  `args` array is written happily and then dropped the next time Claude Code rewrites
  `settings.json` itself, leaving a bare `python.exe` waiting on stdin. The command runs
  through Git Bash, which eats backslashes, so a `C:\Users\...` path arrives with its
  separators gone. And Python encodes stdout in the console code page, so a `·`
  separator reaches the interface as a replacement character. None of this errors: the
  status line is simply blank, quota stays `UNKNOWN`, and the router quietly behaves as
  if the window were nearly spent. `ruti doctor` now checks for all three.
- **`rate_limits.*.resets_at` is Unix epoch seconds, not an ISO string.** Worth stating
  because the fabricated test payload used to build this feature had it the other way
  round, so the mistake survived until the first real reading arrived.
- **`lms load --estimate-only` is a stub.** It returns the model's file size, unchanged
  between 4096 and 131072 tokens of context and between `--gpu off` and `--gpu max`.
  `ruti` computes the KV cache from GGUF headers instead, then corrects itself against
  the measured VRAM delta after every load.
- **`lms ls --json` reports `path` inconsistently** — a real file path for a plain
  download, but just the model key for one with variants, and bundled models live under
  a different root. Trusting it silently drops memory planning back to a crude table.
- **LiteLLM's `/model/new` needs a database.** Without one it returns 500 before any
  auth check, so runtime model registration is not available. `include:` solves the
  same problem better: ruti owns generated files, the hand-written config keeps its
  comments, and a model swap needs no proxy change at all.
- **`/health` hangs** — it dials every backend, including the dead one you are trying
  to diagnose. Use `/health/liveliness`.
- **LiteLLM binds to `0.0.0.0` by default** and answers `/model/info` without auth. On
  a laptop that joins public networks, that hands your model list and your API budget
  to anyone who can route to you.
- **Not every "popular" router is real.** Two candidates with tens of thousands of
  GitHub stars turned out to be months-old repos under single personal accounts,
  cross-promoted on content farms, with README claims contradicting Anthropic's
  documented behaviour. Check `created_at` and `owner.type` before you
  `npm install -g` something that collects every API key you own.
- **A pasted "top free coding models" list is half real at best.** The one this
  feature was built from — AI-generated, complete with citation markers — mixed
  genuine slugs with plausible-looking inventions. `ruti openrouter models` ships a
  shortlist that was checked against OpenRouter's `/api/v1/models` and always merges
  it with a live query, so a slug that has since vanished shows as `missing` rather
  than 404-ing mid-delegation. The routing endpoints (`openrouter/pareto-code`,
  `openrouter/free`) were confirmed against OpenRouter's own docs.
- **Qwen Code CLI is not viable for local models.** Its system prompt is ~31k tokens.
  On a local 8B model that took 8 minutes and still timed out.

## Layout

```
ruti/                       the CLI
  proc.py                   the only place a subprocess is spawned - stdin closed, timeout always
  lmstudio.py  gguf.py      what models exist, and what they cost in memory
  vram.py      planner.py   fit arithmetic, and the load-alongside-or-evict decision
  providers.py openrouter.py  the provider catalog, the key test, and OpenRouter's live model list
  quota.py     statusline.py  the budget, and the only place Claude Code reveals it
  router.py    delegate.py  who does the work, and running them without paying for the noise
  sessions.py  modes.py     the per-session kill switch and the coding / free toggles
  doctor.py    install.py   the silent failures, and wiring into Claude Code
claude/CLAUDE.md            the policy that makes Claude route rather than type
claude/agents/              delegate-runner, delegate-verifier
litellm/config.yaml         hand-written; the generated lists are pulled in via include:
hooks/on-pro-limit.ps1      notifier for when the limit is hit anyway
```

## License

MIT
