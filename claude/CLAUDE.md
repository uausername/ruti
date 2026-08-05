# Copy into ~/.claude/CLAUDE.md (or append to your existing one).
# This is what actually makes the delegation happen - without it, Claude Code has no
# reason to shell out to `opencode` instead of writing the code itself.

## Delegate implementation-heavy work to conserve Pro subscription quota

This machine runs a local router (`C:\mycode\ruti`) so you can offload token-heavy work to
non-Anthropic-billed backends without leaving the current session. `opencode` talks to a
LiteLLM proxy on `http://localhost:4000`, which routes to a local LM Studio model and falls
back to Gemini (rotating between two API keys). The proxy auto-starts at logon via the
`RutiLiteLLM` scheduled task.

Default to delegating rather than writing everything yourself when a task is substantial
implementation work: generating boilerplate, repetitive multi-file edits, straightforward
refactors, or any single self-contained chunk of code generation that doesn't require deep
back-and-forth judgment. Use the Bash tool:

```
opencode run --model ruti-router/lm-studio-local "<precise, self-contained instructions>"
opencode run --model ruti-router/gemini-flash "<precise, self-contained instructions>"
```

`opencode run` writes files itself, so run it from the directory the work belongs in and
review the resulting diff — you stay responsible for correctness. Prefer the local model for
routine work; switch to `gemini-flash` when the task needs stronger reasoning or a bigger
context than the local model's window.

Keep for yourself (do not delegate): planning and architecture decisions, anything requiring
back-and-forth exploration of the codebase first, security-sensitive code, and final review.

The Bash tool's default timeout is 2 minutes and local-model runs regularly exceed it; pass an
explicit longer `timeout`, or let Claude Code background the command and poll it, rather than
assuming it failed.

If a delegated call fails because the local model isn't loaded or the proxy isn't running, do
the work yourself instead of retrying repeatedly.

### Swapping the local model

Only one model fits in this machine's 6 GB of VRAM at a time. To change it:

```
lms unload --all
lms load <model> --gpu max --context-length 32768 --identifier <name>
```

Then point `model:` in `C:\mycode\ruti\litellm\config.yaml` at `openai/<name>` and restart the
proxy (`Stop-ScheduledTask`/`Start-ScheduledTask -TaskName RutiLiteLLM`). Note that not every
model handles agentic tool-calling reliably — Qwen2.5-Coder emitted tool calls as plain text
here, while Llama-3.1-8B works.
