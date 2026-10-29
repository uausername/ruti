# Independent review and resolutions

2026-10-29. All reviewer runs used Codex subscription CLI through Harness in
separate local clones. Neither reviewer launched Claude or another agent.

First reviewer: `native-process-review`, `bd64271f-1a06-4fe4-acfc-cdb7d38268ea`,
reviewed Ruti `8d384ed..b479d59`, HACCP `939ce1b..8034078`, ran 84 isolated
Ruti focused tests, and reported:

| Finding | Evidence | Resolution |
|---|---|---|
| P1 native recovery self-blocks | exec_command/cmd was renamed to Bash but not mapped to command | b305565 normalizes before both gates; native alias regression tests |
| P2 capture loses old quota | quota record outside bounded tail was erased on next capture | b305565 merges validated own cache without refreshing old observation time |
| P2 other-session prefixed lookup | populated codex:other bypassed current-session comparison | b305565 requires exact current raw ID for all load forms |
| P2 HACCP clone lacks PR tooling | manager clone defaulted to main | 7fb995a pins published source HEAD before reading cloned instructions |

Follow-up reviewer: `native-fixes-review`,
`f3574b55-1d74-4170-9b7f-091d75ec7bda`, inspected b305565/7fb995a and confirmed
all four resolutions. It ran **90 Ruti focused tests** with temporary RUTI_HOME
and **19 HACCP fixtures**. Its first Ruti invocation without that override had
**82 passed, 8 failed** because import-time LOCK_DIR/LOG_DIR still targeted real
state in a read-only environment. That finding is fixed by e315bdb; parent
verified **99 focused tests passed** with RUTI_HOME pointing to an unwritable
/proc location. No failed invocation is represented as passing.

Parent additionally reviewed session-bound Bash/PowerShell recovery commands:
0df0da9 strips only the prescribed host/session bindings for gate inspection,
never executes or automatically approves them. Other session IDs, substitutions
and appended commands stay gated. Final full suites: **1019 Windows passed**;
**1010 Linux passed, 6 skipped**. Sanitized XML accompanies the report.

The person's WSL hook trust was granted through the native UI. Parent observed
all five real lifecycle event receipts tied to the first reviewer's exact
session/cwd. This establishes execution of the lifecycle adapter, not a real
five-hour wait, forced flow handoff, automatic model switch, or trusted HACCP
project guard. Windows native CLI hook trust remains separately unverified.

Both reviewers closed successfully. A follow-up `harness tell` from the outside
parent returned NOT_YOURS; this was recorded and not bypassed. The replacement
reviewer used a new Harness-created project. No `/code-review` or
`/security-review` skill invocation is claimed by these reviews.
