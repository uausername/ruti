# Stage C — working Windows Ruti footer and cross-platform validation

Historical stage snapshot. See [stage D](STAGE_D_REPORT.md) for publication,
native Codex hooks, verified own-session metrics and the HACCP process transfer.

Date: 2026-10-29. **Working Windows demo: PASS.**
HACCP deployment remains **NO-GO**; the next bounded collaboration pilot is viable.

## Delivered

- Preserved the existing pinned Codex 0.164.0 patch, installer, source-build script,
  stock fallback and session-host changes. Imported the reviewed stage B Python
  renderer and its tests from the independent Claude-manager's Linux worktree.
- Added `ruti.footer_facts`, implemented by a Codex child through OpenHarness in
  a standalone local clone, then reviewed and integrated by the parent Codex.
  This stage did not start Claude as a child or send work to the person's manager.
- The CLI entry point requests a detached facts refresh after 20 seconds; rendering
  remains cache-only. The worker uses localhost proxy liveliness, LM Studio and
  `nvidia-smi`, without model or subscription-quota requests. `loaded_ok` separates
  a verified empty model list from missing data. Unknown/stale values remain explicit.
- Parent review added a nonblocking OS advisory guard around lease replacement:
  concurrent stale-lock recovery cannot delete a successor's lease. Added actual
  concurrent recovery tests and Windows integration tests for the CLI entry point.
- Added `scripts/start_codex_ruti.ps1`, forwarding arguments to the already
  installed custom executable, even when the current shell PATH selects stock Codex.
- Fixed an existing Windows test fixture: its Bash redirect used an unquoted path
  containing spaces. It now quotes a forward-slash path with `shlex.quote`.
  Timemachine production behavior was not changed.

## Actual evidence

| Check | Result |
|---|---|
| Windows Python 3.12 full pytest | **920 passed**, zero failures/errors, 191.96s |
| Debian WSL Python 3.13 full pytest | **911 passed, 6 skipped**, zero failures/errors, 33.32s |
| Focused Windows footer/worker/installer tests | **58 passed** |
| Compileall | PASS, Windows and Linux |
| Standalone worker/session-isolation live check | PASS, isolated state, real localhost/GPU probes, no model request |
| Launcher syntax and `--version` | PASS: `codex-cli 0.164.0` |
| Custom installed binary SHA256 | Matches the existing pinned artifact manifest |
| Native Windows Codex TUI | Started through a PTY; the actual Ruti item displayed local service/GPU state |
| Codex config preservation during demo | SHA256 unchanged from backup |

Evidence artifacts: [Windows JUnit](windows-final.xml), [Linux JUnit](linux-final.xml),
[isolated live check](windows-live-footer.json). Hostname was removed from JUnit;
failed-baseline tracebacks and raw terminal/auth output remain outside tracked docs.
These runs are local validation, not a remote CI pass. Linux warnings include existing
Click deprecations and a Pydantic annotation warning; six platform skips are explicit.

The live isolated check produced:

```text
first:     Ruti on timemachine* proxy unknown local:unknown GPU unknown
session A: Ruti OFF timemachine* proxy up local:none GPU5.5G
session B: Ruti on timemachine* proxy up local:none GPU5.5G
```

Initial invocation: 0.422s; subsequent invocations: 0.469s and 0.390s. The first
entry point returned before the worker populated the cache. The cache populated
within the ten-second observation deadline. No Claude refresher was used for this
isolated state. Real GPU free memory is a point-in-time observation, not a fixture.

Actual native TUI text was observed through the Windows PTY, including:

```text
Ruti on coding proxy up local:none GPU5.5G
Ruti on coding wait* flow* manager* timemachine* proxy up local:none GPU5.5G
```

The 80-column PTY truncated the remainder of the combined native footer. These are
terminal text observations, not a screenshot or proof at every width. Codex's own
native five-hour limit item also populated; the Python renderer does not read
Claude quota or fabricate corresponding values.

## Build provenance and configuration

The installed custom executable and the existing build artifact share SHA256:
`87d83fd363c208574822a3fe4c898442cba584148217d53e2089265fca8d3f4b`.
The patch SHA256 matches:
`2fcf70165b5486be1d5693a7b93c0e89543dce65a552079edc4856d25803a6e9`.
Pinned upstream commit: `6aaac694803004a63c6f393d153c96d22521a6b9`.
The pre-existing build log records a successful `dev-small` build; no Rust source
changed in stage C, so an identical Rust rebuild was not required or claimed.
The custom binary is already installed alongside a stock fallback with the original
installer backups. Stage C did not replace the binary or change provider/auth settings.

A backup of the current Windows Codex config was saved before the TUI check;
its hash remained identical afterward. Linux dependency installation was confined
to the existing isolated venv. LiteLLM 1.106.6 was installed there from the project's
declared dependency list. The prior 14 Linux setup errors were caused by missing
LiteLLM: without the installed package, Python saw only the repository's namespace
directory. They did not persist after installing the dependency.

## Harness lifecycle and collaboration scope

The prior stage B independent Claude-manager → Codex-child → manager test/review
loop remains recorded in [STAGE_B_REPORT.md](STAGE_B_REPORT.md). Its historical
test counts are superseded by the clean environment and runs above.

Stage C additionally exercised an owned Codex child:
`ruti-facts-pilot`, id `678aea1b-8690-450b-a830-96dcaa2c064a`.
It prepared a standalone local clone inside a Harness-created project, produced
only the two allowed new files, and ran 32 focused tests. Parent review inspected
the actual files, added fixes/tests, then ran both complete suites. Closing this
owned child succeeded. An earlier owned worktree attempt waited for trust and
was stopped/closed; no trust setting was changed to force it through.
This demonstrates the standalone-clone route and owned close lifecycle.
It does not resolve stage B's separate Claude identity/NOT_YOURS incident.

An additional independent Codex read-only review ran as `ruti-footer-review`,
id `3d16acf2-9755-483f-9373-872fe3dcd23a`. It found no default-installation
blocker and one P2: the launcher ignored an explicit `RUTI_HOME`. Parent fixed
that in `0f59eb6` and actually checked default launch and missing custom-install
behavior in PowerShell. The reviewer performed static inspection, not a test
rerun. [Full review and resolution](CODEX_FINAL_REVIEW.md). Its owned close
also succeeded; none of this task's Harness children remain open.

## Remaining limits before HACCP GO

- The custom footer is tied to this patched Codex release; stock Codex cannot load
  an arbitrary Ruti status command. Future Codex versions need a reviewed patch/build.
- One compact line is implemented. Claude's three-line layout, color parity and
  all `show/hide` view semantics are not implemented in the native Codex item.
- Starred modes describe stored settings; their Claude-hook automation is not
  implemented for Codex. No full live mode matrix or remote CI claim is made.
- Python still has no verified Codex five-hour/weekly metric source; use native
  Codex items for those values. WSL proxy/GPU live reachability remains separate
  from the successful native Windows runtime check.
- A new independent Claude review/fix cycle, repeated reliability/approval drills,
  and the Claude close-ownership issue still belong to the next collaboration pilot.
  The primary purpose here was to make the Ruti rehearsal runnable and preserve it.

Run from PowerShell in the Windows checkout:

```powershell
.\scripts\start_codex_ruti.ps1
```

Local source commits on `pilot/openharness-bootstrap-20261029`:

- `d0d4f4b` — correct the Windows Bash-redirect test fixture.
- `2b58a79` — native footer, independent worker, installer, launcher and tests.
- `3f7b329` — preserve the existing router alias list that matches the running proxy.
  This JSON was validated; individual remote models were not called for this check.
- `0f59eb6` — fix custom `RUTI_HOME` launcher selection after independent review.

Historical stage B files are also preserved as `c75608a` on `pilot/statusline-codex`.
No push, merge into main, or change to the HACCP repository was performed.
