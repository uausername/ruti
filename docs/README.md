# docs

This project started from a research proposal on routing Claude Code CLI traffic. The
document itself is not in this repository — it is private — but what it recommended,
and what happened to that recommendation, is worth recording.

**Its central proposal did not survive contact with the tool.** The plan was to put a
router proxy in front of Claude Code and fall back to a local model on HTTP 429. That
is impossible: pointing `ANTHROPIC_BASE_URL` at a router disables Claude Code's OAuth
entirely, so the subscription stops applying and the CLI demands a pay-per-token API
key instead. Auth is read once at process start and cannot be swapped at runtime.
Anthropic's terms prohibit the arrangement independently. The main README covers this
in full.

**What did hold up:** LM Studio as the local serving layer, sizing quantized models
against the available VRAM rather than hoping, and keeping the routing policy in data
rather than in code.

The delegation approach that shipped here — Claude Code stays exactly as it is and
gains a *tool* that is another coding agent — is what remained once the proxy idea was
ruled out.
