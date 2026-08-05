# ruti

**Stop burning your Claude Code Pro quota on boilerplate.**

`ruti` keeps Claude Code as your architect and reviewer, and hands the token-heavy typing to a
local model on your own GPU — or to Gemini's free tier — *without leaving your session*.

No proxy in front of Claude. No re-authentication. No lost context.

---

## The problem

You're on Claude Code Pro ($20/mo). You hit the rolling 5-hour usage limit, and now you wait.
Meanwhile you have a GPU sitting idle and free API tiers going unused.

The obvious fix — point `ANTHROPIC_BASE_URL` at a router that falls back to a local model — **does
not work**, for two independent reasons:

1. **Technically:** setting `ANTHROPIC_BASE_URL` disables Claude Code's OAuth entirely. Your Pro
   subscription stops applying; the CLI demands a pay-per-token API key instead. Auth is read once
   at process start and cannot be swapped at runtime.
2. **Contractually:** Anthropic's terms (Feb 2026) prohibit routing Free/Pro/Max subscription
   credentials through third-party tools.

Hooks don't rescue it either. `StopFailure` fires *after* a rate-limit error and can only log or
notify — it cannot intercept the turn or substitute another model's response.

So "seamlessly continue this conversation on a different backend" is architecturally impossible.

## The insight

Don't switch Claude's backend. **Give Claude a tool that is another coding agent.**

Claude Code stays exactly as it is — same OAuth, same Pro subscription, same unbroken session. It
just learns to shell out to `opencode` (a provider-agnostic coding CLI) for the expensive parts,
then reviews what came back. Because that's an ordinary Bash tool call, nothing about your session
changes: no restart, no context loss, no terms violation.

Claude spends tokens on *thinking*. The delegate spends tokens on *typing*. Only one of those is
billed to your subscription.

## Architecture

```
Claude Code  (Pro subscription, OAuth, untouched)
     │
     │  delegates via Bash, per instructions in ~/.claude/CLAUDE.md
     ▼
opencode run --model ruti-router/<target>
     │
     ▼
LiteLLM proxy  :4000        ← auto-starts at logon
     ├──► LM Studio :1234   → local model on your GPU        (free, private)
     └──► Gemini Flash      → rotates 2 API keys, 60s cooldown on 429
```

The proxy is the only piece that knows about credentials. Swap models, add providers, or rotate
keys there and nothing downstream needs to change.

## What you get

| | |
|---|---|
| **Your Pro session is never interrupted** | Claude Code runs unmodified — no env vars, no proxy in front of it |
| **Local work is free and private** | Code never leaves the machine when routed to LM Studio |
| **Free tiers, stacked** | Two Gemini keys from separate projects rotate automatically; a 429 cools one down for 60s |
| **Automatic fallback** | Local model down or overloaded → request transparently retries on Gemini |
| **Survives reboots** | The proxy runs as a scheduled task, not a terminal you have to babysit |
| **Model swaps are one line** | Change the identifier in `config.yaml`; nothing else moves |

## Requirements

- **Claude Code** with a Pro/Max subscription
- **LM Studio** ≥ 0.4 (this setup was built on 0.4.16) + `llmster` for a scriptable `lms` CLI
- **Python 3.12** for LiteLLM, **Node.js** for OpenCode
- A GPU with enough VRAM for your model — reference machine is an **RTX 3060 Laptop, 6 GB**
- One or more **Google AI Studio** API keys (free tier is fine)

## Setup

**1. Local model**

```powershell
irm https://lmstudio.ai/install.ps1 | iex     # installs llmster + a working `lms` CLI
lms get "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"
lms server start
lms load meta-llama-3.1-8b-instruct --gpu max --context-length 32768 --identifier local-llama31
```

**2. Proxy**

```powershell
pip install "litellm[proxy]"
cd litellm
cp .env.example .env        # then put your real Gemini keys in it
```

**3. Delegate CLI**

```powershell
npm install -g opencode-ai
mkdir "$env:USERPROFILE\.config\opencode"
cp opencode\opencode.json "$env:USERPROFILE\.config\opencode\opencode.json"
```

**4. Teach Claude Code to delegate** — append `claude/CLAUDE.md` to your `~/.claude/CLAUDE.md`.
Without this step nothing delegates; Claude has no reason to prefer `opencode` over doing the work
itself.

**5. Auto-start the proxy at logon**

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\mycode\ruti\litellm\start-litellm.ps1"'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName "RutiLiteLLM" -Action $action -Trigger $trigger -Force
Start-ScheduledTask -TaskName "RutiLiteLLM"
```

**6. Optional — get told when you hit the limit anyway**

Register `hooks/on-pro-limit.ps1` as a `StopFailure` hook in `~/.claude/settings.json` (see
`hooks/README.md`). It pops a reminder that the delegates are still available.

**Verify:**

```powershell
curl http://localhost:4000/health/liveliness          # "I'm alive!"
opencode run --model ruti-router/lm-studio-local "write hello world to hello.py"
```

## Choosing a local model

The hard requirement is **reliable structured tool-calling** — the delegate has to actually *call*
the write-file tool, not describe calling it. Not every model manages this, and it is not
correlated with how good the model is at code.

Measured on the reference machine (RTX 3060, 6 GB VRAM):

| Model | Fits in 6 GB | Speed | Agentic tool-calling |
|---|---|---|---|
| **Llama-3.1-8B-Instruct** Q4_K_S | ✅ fully on GPU | ~5.6 s | ✅ **works** |
| Qwen2.5-Coder-14B Q4_K_M | ⚠️ partial CPU offload | ~15 s | ❌ emits tool calls as plain text |
| Qwen2.5-Coder-14B Q2_K | ✅ | ~8 s | ❌ empty responses |

Llama-3.1-8B is the recommended starting point. If you swap it, re-run the verify step above and
confirm a file actually appears on disk.

## Notes from the build

Things that cost time here, so they don't cost you any:

- **Not every "popular" router is real.** Two candidates with tens of thousands of GitHub stars
  turned out to be months-old repos under single personal accounts, cross-promoted on content
  farms, with README claims contradicting Anthropic's documented behaviour. Check `created_at` and
  `owner.type` via the GitHub API before you `npm install -g` something that collects every API key
  you own.
- **Qwen Code CLI is not viable for local models.** Its system prompt is ~31k tokens (OpenCode's is
  ~7.5k). On a local 8B model that took 8 minutes and still timed out. Prompt size belongs to the
  *harness*, not the model — a heavyweight CLI will sink any local backend.
- **LM Studio's bundled `lms` CLI (pre-0.4) hangs when scripted.** Install `llmster` for a CLI that
  works non-interactively.
- **LiteLLM's banner crashes on non-UTF-8 Windows consoles.** `PYTHONIOENCODING=utf-8` fixes it;
  the launcher script sets it for you.
- **`master_key` in LiteLLM's config requires a database.** Omit it for single-user local use or
  every request fails with `No connected db`.
- **Watch `request_timeout`.** The default 10s silently falls back to Gemini before a local model
  can finish, which looks exactly like the local model being broken.

## Layout

```
claude/CLAUDE.md            instructions that make Claude Code delegate
litellm/config.yaml         model list, key rotation, fallback chain
litellm/.env.example        template for your keys (.env is gitignored)
litellm/start-litellm.ps1   launcher used by the scheduled task
opencode/opencode.json      points OpenCode at the proxy
hooks/on-pro-limit.ps1      notifier for when the Pro limit is hit anyway
```

## License

MIT
