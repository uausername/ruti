# Native Codex automation and HACCP transfer — 2026-10-29

## Publication

Both requested histories were committed and pushed:

- `pilot/openharness-bootstrap-20261029`: `6032e51`, includes the original brief.
- `pilot/statusline-codex`: `d66ec24`, Claude stage B plus sanitized XML evidence.
- `feature/codex-native-hooks-20261029`: native automation and review fixes;
  [PR #63](https://github.com/uausername/ruti/pull/63).

No private transcripts/credentials were published. The dirty existing HACCP
checkout C:/mycode/haccp was preserved; tooling uses a separate clone.

## Native Codex implementation

Pinned Codex 0.164 supports lifecycle hooks, confirmed against source and the
[official Hooks guide](https://learn.chatgpt.com/docs/hooks). This supersedes
stage C's historical assumptions about missing hooks and metric sources.

- Bounded exact-session rollout reader/cache supplies native 5h/weekly/context
  observations, retaining source timestamps. Windows are identified by their
  300/10080-minute duration. Context follows Codex's last-turn/baseline formula.
  No Claude quota, transcript prose, credentials or invented percentages.
- Opt-in `ruti install --host codex --native-hooks`, preview by default, merges
  hooks and backs up existing files. It never grants trust or changes approvals.
- Separate native policy adapter: SessionStart/UserPromptSubmit guidance,
  PreToolUse/PostToolUse gates, apply_patch time-machine warnings including
  moves, and supported Stop behavior. Flow filenames support Windows safely.
- Read-only `ruti codex status --json` and `ruti codex pending --json` expose
  runtime and manager-required checkpoint receipts.

## Executed verification

| Check | Actual result |
|---|---|
| Full Windows Python 3.12 suite after fixes | **1019 passed**, 171.47s |
| Full WSL Python 3.13 suite after fixes | **1010 passed, 6 skipped**, 33.94s |
| Installed commands through Windows COMSPEC/cmd | All five commands executed in isolated state |
| Installed commands through WSL login shell | All five commands executed in isolated state |
| Existing Windows/WSL hooks preserved | Non-Ruti definitions equal parsed backups |
| Real own Codex rollout | 5h/weekly/context read; original observation time retained |
| WSL native exact-definition hook trust | Person confirmed; all five real lifecycle events observed |
| Windows native CLI hook trust | Not verified; command fixtures only |
| Independent Claude manager in this stage | **Not launched**; person-only launcher prepared |

Raw XML/logs remain under ignored build/openharness-bootstrap. Command fixtures
are not evidence of native CLI trust. Live definitions were installed in both
OS profiles with backups: Windows
`~/.codex/hooks.json.ruti-backup-20261029-190209`, WSL
`~/.codex/hooks.json.ruti-backup-20261029-170211`. WSL Codex AGENTS.md was a new
file; Windows guidance was already current. Existing Harness hooks, subscription
auth and project-trust config were preserved. The person subsequently granted
WSL's exact hook-definition trust through Codex's UI; the agent did not write it.

Backup exception: a separate WSL config.toml snapshot was not taken immediately
before that manual UI confirmation. Codex writes hook trust under hooks.state
in this file. The installer had backed up hooks.json, but that does not replace
a pre-trust config.toml backup. Current private snapshots were saved afterward
(`config.toml.ruti-snapshot-20261029T174115255263Z` in WSL,
`config.toml.ruti-snapshot-20261029T174114962238Z` on Windows). These are explicitly
post-trust/current snapshots, not reconstructed or falsely labeled old backups.
Subscription authentication was not replaced; no config contents were published.

## Mode boundaries

| Feature | Behavior and limit |
|---|---|
| on/off, coding, free | Per-session policy and existing routing/delegation enforcement |
| wait | Native measured tool gate and same-session Stop continuation; full real reset sleep unobserved |
| flow | Measured gate and checkpoint receipt; independent manager starts continuation, no nested spawn |
| timemachine | Native startup/direct-tool warnings with bounded advisory work |
| manager, eff+ | Advice; hooks cannot change active model/effort |
| council, Jev | Configured policy, no implicit paid API calls |
| footer | Cache-only; native runtime receipts can unstar wait/timemachine; partial modes stay starred |
| Complete autonomous parity | **Not achieved or claimed** |

Never launch Claude as Codex's child. Runtime receipts are historical evidence,
not proof that every current definition is trusted.

## Harness and HACCP

Parent inspected actual child changes, corrected portability/validation issues,
ran the suites and closed these owned Codex implementation children successfully:

- `ruti-native-telemetry`, `a1388f33-7dce-4e41-b172-2cd885322fe8`.
- `ruti-native-installer`, `fa78432a-397c-4410-88f1-56c9f8912d90`.
- `haccp-process-preflight`, `4c7f2f57-80fb-40c2-9210-584896a070bc`.

Independent `native-process-review`, `bd64271f-1a06-4fe4-acfc-cdb7d38268ea`,
initially stopped at «Hooks need review». The person confirmed the five Ruti
definitions in a directly attached Windows Terminal/WSL pane. Parent then
observed exact-review-session receipts for SessionStart, UserPromptSubmit,
PreToolUse, PostToolUse and Stop, plus native quota/context measurements.

The first review found recovery `cmd` normalization, bounded-tail quota loss,
cross-session loading, and the HACCP launcher's unpinned clone. Fixes are in
Ruti `b305565` and HACCP `7fb995a`. A fresh independent `native-fixes-review`
(`f3574b55-1d74-4170-9b7f-091d75ec7bda`) started without another trust prompt,
confirmed all four resolutions, ran 90 Ruti and 19 HACCP focused tests in
temporary state, and found an additional test-isolation defect. Parent fixed
import-time lock/log bindings in `e315bdb`; 99 targeted tests then passed with
an explicitly unwritable original RUTI_HOME. Parent also added session-bound
Bash/PowerShell recovery prefixes and chaining/isolation regressions in
`0df0da9`, then reran both full suites above. Both owned reviewers closed
successfully. [Review details](STAGE_D_REVIEW.md).

One real Harness limitation remains: `harness tell` from the outside Codex
parent returned NOT_YOURS, although new/wait/show/close worked. No identity or
send bypass was used; the follow-up used a new reviewer. The top-level manager
must test tell/close from its own pane before relying on unattended orchestration.

HACCP instructions and specification section 14 were read. Claim
[PR #631](https://github.com/uausername/haccp/pull/631) passed its checks and
squash-merged as `939ce1b`. Tooling is published in
[draft PR #632](https://github.com/uausername/haccp/pull/632); the task stays in
progress pending project review/rehearsal gates. It adds project guidance, read-only preflight,
protected-file guard, backup installer, CI fixtures and an independent-manager
launcher that refuses agent environments.

HACCP tooling: **19 tests passed on Windows and WSL**. Preflight passed in
Windows and a fresh WSL ext4 clone. The WSL /mnt/c status probe timed out and
correctly reported unknown/failure. The local Windows project hook definition
is installed and gitignored; native trust is still manual. Claude configuration
is untouched. The guard covers direct file edits; shell/MCP writes remain outside
its scope. It is not a sandbox. Product, real data and production are unchanged.

HACCP `flutter pub get` and the CI-prescribed `dart run build_runner build`
completed in the fresh clone, then `flutter analyze --no-pub` reported
**No issues found** (32.8s). Earlier attempts without dependencies/generated
sources failed; they were not treated as product defects or passing checks.
The committed `scripts/verify-migrations.sh` ran against a dedicated ephemeral
Postgres 16 Docker container with **no network and no published ports**:
**106 migrations applied clean**. The container was stopped/removed afterward.
Initial copying of the Windows CRLF script failed; the successful run used its
committed LF bytes. No existing database or production endpoint was used.

## Person's exact next steps

The WSL global-hook trust step is complete; both review children are closed.
The independent Claude manager must still be started by the person, as requested.

Then from a **fresh WSL Debian terminal outside any agent**:

```bash
cd /mnt/c/mycode/ruti/build/haccp-harness-pilot
bash scripts/agent-pilot/start_manager.sh
```

The launcher starts independent Claude in `ask` mode with the bounded rehearsal
brief pinned to the published HACCP source revision. Do not invoke it from a
Codex/Claude tool shell. Project protected-file hooks require their own `/hooks`
review. The full protocol and evidence rules are in HACCP docs/agent-pilot.md.
The HACCP PR remains draft until its prescribed project review and bounded
manager rehearsal; independent Codex review is not claimed as `/code-review`.
