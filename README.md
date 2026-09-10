# ruti

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
| **OpenRouter coding models, one command** | `ruti openrouter models` merges a vetted shortlist with OpenRouter's live catalogue; `ruti openrouter setup` registers the `pareto-code` / `free` routers and any `:free` models you pick as routable aliases |

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
# RunLevel Highest matters: litellm/.env is locked down to SYSTEM/Administrators
# (see `ruti doctor`'s secrets check), and a task registered without it can never
# read its own keys. Some machines block elevated token duplication for a plain
# user account outright, even with this set correctly -- if `ruti doctor` still
# shows the proxy down after logon, `ruti doctor --fix` falls back to a direct
# elevated launch (one UAC prompt) instead of requiring a rebuild of this task.
$action    = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\mycode\ruti\litellm\start-litellm.ps1"'
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "RutiLiteLLM" -Action $action -Trigger $trigger -Principal $principal -Force
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
