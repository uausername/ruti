# Independent read-only footer review

Parent resolution: the P2 launcher finding below was fixed in `0f59eb6`.
Actual PowerShell checks passed for the default `--version` launch and for
rejecting a missing installation under an explicit custom `RUTI_HOME`.
The implementation-notes link is included in the final documentation commit.
The reviewer did not independently rerun those parent checks.

Reviewed branch `pilot/openharness-bootstrap-20261029` at `3f7b329da342cd61b1c091c7362995548e7c3342`, including expected feature commit `2b58a79`. Source is unchanged in the standalone local clone at `repo/`.

No blocking findings for the described default Windows installation.

## Finding

**P2, nonblocking for the default installation: launcher ignores the configured state root.** `scripts/start_codex_ruti.ps1:4` always selects `%LOCALAPPDATA%/ruti`, while `ruti/codex_footer_install.py:164` installs under `STATE_ROOT`, which honors `RUTI_HOME` (`ruti/config.py`, `_state_root`). With `$env:RUTI_HOME = 'C:\RutiAlternate'`, an installation goes to `C:\RutiAlternate\codex-cli\0.164.0-ruti\codex.exe`, but this launcher still looks under LocalAppData. It throws if that default copy is absent; if present, it launches that other copy. This is a direct static reproduction from the two path expressions; I did not execute a Windows installation or PowerShell. Resolve the launcher root from `RUTI_HOME` first, falling back to LocalAppData consistently with the installer.

## Correctness and safety assessment

- The native patch runs Python asynchronously with a four-second timeout, bounded stdout, null stdin/stderr, `kill_on_drop`, absolute interpreter selection, isolated Python mode, and an interpreter-directory working directory. Rendering does not directly invoke HTTP or GPU probes. The Python CLI requests a detached facts worker before reading the cache.
- Worker HTTP requests use fixed loopback addresses, disable environment proxies and redirects, and bound response size. GPU execution is confined to the worker and uses the existing process timeout wrapper. No inference endpoint is called.
- Cache timestamps are checked for finite numeric values and future timestamps; old facts do not retain healthy badges. Successful empty model observations are distinguished from failed observations.
- The nonblocking OS guard serializes lease replacement; expiring token leases recover from worker crashes. Workers recheck ownership under the guard before publishing, and cleanup checks the token, protecting successor leases.
- Session modes, OFF state and delegates use the displayed Codex identity; native responses for another thread are rejected. Host inference preserves an explicit host override.
- The footer does not read or fabricate provider subscription quota, and the feature diff does not alter provider credentials or quota implementation. The alias-list follow-up changes model names only. Installation validates binary/patch manifest hashes, preserves unrelated parsed TOML settings, backs up replaced files, and detects concurrent PATH changes. Hash validation is consistency checking of the supplied local build, not an independent signature.

## Limitations

- Requires the custom binary pinned to Codex `rust-v0.164.0`; later releases require patch review and rebuilding.
- Claude hook automation is unsupported on Codex; starred modes report settings rather than implementing those hooks.
- Python supplies no Codex subscription quota; Codex's native usage items remain responsible for it.
- Python cached JSON reads and delegate-directory enumeration are not intrinsically size bounded; the native asynchronous timeout bounds their effect on the TUI. Tightening these reads would be future hardening, not an observed blocker.
- Windows runtime/binary behavior was not independently exercised here. The parent-provided Windows/Linux suite and live TUI results are external evidence, not tests I ran.

## Inspection and testing performed

Read in full: `ruti/footer_facts.py`, `ruti/footer.py`, `ruti/codex_footer_install.py`, `scripts/start_codex_ruti.ps1`, `tests/test_footer.py`, `tests/test_footer_facts.py`, `tests/test_codex_footer_install.py`, `tests/test_codex_host.py`, `ruti/config.py`, `ruti/runs.py`, `ruti/sessions.py`, `ruti/modes.py`, `ruti/proc.py`, `ruti/statusline.py`, the native `codex/patches/statusline-ruti-0.164.0.patch`, `scripts/build_codex_statusline.py`, `docs/codex.md`, and `pyproject.toml`. Inspected feature commit statistics, CLI/session diffs, and the alias-list follow-up diff. The implementation-notes file linked from `docs/codex.md` is absent from this clone.

No tests, probes, services, inference requests, installers, builds, or live-state operations were run. No full-suite rerun. Verification consisted of static inspection, Git history/diff/status reads, and local cloning. No delegation, source edits, commits, pushes, or merges.
