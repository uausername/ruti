# Native Codex automation and HACCP process pilot

2026-10-29. Work follows the pinned Codex 0.164.0 sources and the official
[Hooks guide](https://learn.chatgpt.com/docs/hooks). Earlier pilot reports describe
historical limitations, not a claim that current Codex has no lifecycle hooks.

## Verified foundations

- Both subscription CLIs and Harness work; the independent Claude-manager has
  already created and reviewed a Codex child. Never create that manager as a child.
- Pinned Codex supports SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
  Stop and other native hooks; command hooks require exact-definition user trust.
- Real local Codex rollout files contain session_meta, turn_context and
  event_msg/token_count. The latter contains native primary/secondary windows with
  used_percent, window_minutes and resets_at, and last_token_usage/context size.
  Only windows of 300/10080 minutes may be named five-hour/seven-day. No Claude
  quota, credentials, transcript prose or fabricated measurements may supply them.
- Codex's native TUI estimates context from last_token_usage.total_tokens,
  subtracting the 12000-token baseline, not cumulative session usage.

## Implementation boundaries

1. Bounded, exact-session Codex telemetry reader/cache. Keep each session in a
   hashed per-session file under RUTI_HOME; preserve source observation timestamps.
   Read only a bounded header/tail and whitelist numeric/model metadata. No network.
2. Opt-in native hook installer merging hooks.json, preview by default and timestamp
   backup before changes. Existing user/plugin/Claude configuration stays intact.
   Never write trust hashes or use a hook-trust/approval bypass.
3. Separate Codex hook adapter. Bind payload identity to the Codex namespace;
   inject offline mode policy, update measured quota/context, deliver time-machine
   warnings for native apply_patch, and enforce wait/flow gates. Never invoke the
   Claude manager/seat/flow launcher or auto-spend on Jev/council in this pilot.
4. Stop/resume behavior must use supported native semantics. Wait may continue
   the same session after a measured reset using a synchronous Stop hook. Flow
   must preserve handoffs safely; a Harness child never spawns nested harnesses.
   Any manager-mediated continuation is explicitly marked as requiring the
   independent top-level orchestrator rather than falsely called autonomous.
5. Active model/effort changes are not an output field of these hook schemas.
   Manager/eff+ can automate policy/advice; do not claim a live model switch that
   the CLI has not performed. No invented commands or fake parity claims.
6. Test all logic in isolated state and mocks; run full Windows/Linux suites.
   Live installation/trust is a separate verified gate. Keep exact capabilities
   and remaining limitations visible in reports/footer.

## HACCP scope

Use a fresh clone of origin/main, preserving the dirty existing C:/mycode/haccp.
The current clone is build/haccp-harness-pilot. Follow CLAUDE.md, docs/goal.md,
docs/tasks.yaml and claim protocol. This transfer is process/tooling work, not
permission to change product rules, legal text, real data or production settings.
Prepare a bounded manager/implementer/reviewer workflow with separate clones,
real validation and receipts. Existing founder-only decisions remain founder-only.
No direct main push; project changes go through their prescribed PR/CI gates.

## Publication

Both preceding pilot branches are already pushed to origin. Keep the native
automation on feature/codex-native-hooks-20261029; commit and push tested changes.
Record real test results and manual hook-trust blockers; never mark the whole
matrix or HACCP rollout GO merely because the adapter exists.
