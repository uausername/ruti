# Stage A — OpenHarness bootstrap

Historical bootstrap report. For the current working Windows footer and clean
cross-platform test results, see [STAGE_C_REPORT.md](STAGE_C_REPORT.md).

Date: 2026-10-29. Status: **PASS — stage A bootstrap verified after user sign-in;
ready for an independent Claude-manager to begin stage B.**
Stage B and the Claude-manager → Codex-child live test have not run.

## Repository and preservation

- Repository: `https://github.com/uausername/ruti.git`.
- Initial HEAD: `d5834a59b9cc0f32d67215b3f2064b2410bb6b9a`.
- Initial branch: `main`; pilot branch: `pilot/openharness-bootstrap-20261029`.
- Initial dirty/untracked files are recorded in [BASELINE.md](BASELINE.md).
  They were carried into the pilot branch without changing their contents.
- No source implementation, existing Codex binary, proxy, seat, model, OAuth,
  global defaults or productive branch was changed by bootstrap. No commit/push.
- Project remains on its Windows volume, accessible as `/mnt/c/mycode/ruti`.
  No WSL installation or project migration was performed: Debian WSL2 already existed.
- The existing custom Codex footer work predates this task. A worktree created
  from HEAD will exclude that uncommitted work; the manager must account for it.

## Environment and actual checks

| Check | Result / evidence |
|---|---|
| Native OS | Windows 11 Home x64, build `10.0.26300` (`Get-CimInstance Win32_OperatingSystem`) |
| Shell | Sandbox uses Windows PowerShell 5.1; approved host commands use PowerShell 7.6.9 |
| Windows Claude | `claude --version`: `2.1.312 (Claude Code)`; user `.local/bin/claude.exe` |
| Windows Codex | `codex --version`: `codex-cli 0.164.0`; installed OpenAI Codex application `bin/codex.exe` |
| Windows subscription auth | `claude auth status --json`: loggedIn=true, claude.ai, firstParty, pro; `codex login status`: Logged in using ChatGPT |
| Windows tools | Python 3.12.3, Node v20.16.0, Git 2.46.2.windows.1; no native harness/tmux found |
| WSL | `wsl --list --verbose`: existing Debian, running, version 2 |
| Linux | Debian 13.5, x86_64, WSL kernel `6.18.54.1-microsoft-standard-WSL2`; Python 3.13.5, Git 2.47.3 |
| Initial Linux CLI state | No claude/codex/harness/node/tmux/ruti commands; no `.claude`, `.codex` or `.harness` directories |
| Linux binaries after install | Claude and Codex both answer `--version`; direct ELF-header verification confirms Linux executables under the Linux home, not Windows interop |

The initial sandbox WSL probe returned `E_ACCESSDENIED`, and Python/CLI discovery
was incomplete there. These were sandbox limitations, resolved by approved host
diagnostics; they are not evidence that WSL or Windows Python was missing.

OpenHarness officially supports macOS/Linux and explicitly excludes native
Windows. This installation uses **Debian inside WSL2**, an experimental pilot;
the README specifically names Ubuntu as verified, not this exact Debian/WSL setup.

## Installation and backup

Official installers were downloaded to Linux `/tmp`, read before execution, and
their SHA256 values recorded in the private backup directory. Commands executed:

```bash
curl -fsSL https://harness.autonomous.ai/cli/install.sh -o /tmp/ruti-openharness-install.sh
curl -fsSL https://claude.ai/install.sh -o /tmp/ruti-claude-install.sh
curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/ruti-codex-install.sh
# After inspection and backup:
bash /tmp/ruti-openharness-install.sh
bash /tmp/ruti-claude-install.sh 2.1.312
CODEX_NON_INTERACTIVE=1 sh /tmp/ruti-codex-install.sh --release 0.164.0
```

Installed in the existing Linux user's home:

| Component | Verified version |
|---|---|
| OpenHarness CLI | 0.5.7 |
| Harness managed Node | v22.23.2 |
| tmux (Debian package) | 3.5a |
| Claude Code Linux | 2.1.312 |
| Codex CLI Linux | 0.164.0 |
| hn terminal client (installer output) | 0.1.43 |
| managed grid (installer output) | 0.3.61 |

The Harness installer also installed `wl-clipboard` for the existing WSLg display
and its Debian dependencies: apt reported 73 new packages, 0 upgraded, 0 removed.
Its managed Node is private; no system Node or Windows Node was replaced.

Private backups: `~/.local/state/ruti-openharness-bootstrap/20261029-01a18753/`.
Existing `.bashrc` and `.profile` were copied before changes. Later verification
confirmed that their original contents remain exact prefixes of the new files.
`pre-connect/` contains the then-existing `.claude.json`; absent configuration
paths were recorded before daemon/plugin setup. No Windows auth files were copied.

The installation wrapper ended with exit 1 because the optional `file` utility
was missing, **after all three official installers succeeded**. Installation was
not rerun. Python subsequently verified both executable ELF headers, and all
version checks were rerun successfully.

## Daemon, plugins and MCP

Executed `harness start`, `harness status`, `harness whoami`,
`harness help agents`, and `harness ps`. The daemon is running locally on
`http://127.0.0.1:18473`. Initially signed out; after the user completed sign-in,
Harness confirmed that this Codex agent may start harnesses.

Installed the official plugin in both **Linux** CLI profiles:

```bash
claude plugin marketplace add autonomous-ai/openharness --sparse .claude-plugin plugins
claude plugin install harness@openharness
codex plugin marketplace add autonomous-ai/openharness --sparse .claude-plugin --sparse plugins
codex plugin add harness@openharness
```

- `claude plugin list --json`: harness@openharness, enabled, user scope,
  version `4eca3f07881c`; declares MCP command `harness mcp`.
- `claude plugin details harness@openharness`: one harness skill, one MCP server,
  zero agents and zero hooks in the plugin itself.
- `codex plugin list`: harness@openharness, installed and enabled.
- Codex marketplace checkout: commit `4eca3f07881c28343afa106860501173dcf038d6`.
- A real stdio MCP handshake against `harness mcp` passed `initialize` and
  `tools/list`: server Harness 0.5.7, protocol 2024-11-05, nine tools:
  `harness_whoami`, `harness_list`, `harness_search`, `harness_machines`,
  `harness_start`, `harness_wait`, `harness_read`, `harness_settings`, `harness_close`.
  No MCP tools that start agents were invoked; no model request was made.
- Explicit plugin-browser inspection in an authenticated Codex TUI (`/plugins`)
  and an actual Claude-manager session remain **NOT RUN**. CLI plugin inventory
  and the live MCP server were verified; this is not a claim of TUI visual QA.

With explicit `AI_AGENT=codex` and this conversation's `CODEX_THREAD_ID`,
`harness whoami --json` initially returned `canStart.ok=false`, reason `SIGNED_OUT`,
`fixBy=person`. After the user's confirmation, it returned `canStart.ok=true`
with caller kind `agent`, agent `Codex`. The limits reported were four open
children, twenty starts per ten minutes and maximum child depth one.
No `harness new claude`, `claude -p`, or Claude inference session was launched.
Neither Agent Messages nor Agents can start agents was enabled by Codex.

### Post-login live Codex smoke test — PASS

After the user completed the logins and granted Harness account consent:

- Linux Claude status confirmed a Claude Pro account; Linux `codex login status`
  returned `Logged in using ChatGPT`. No account identifiers are stored here.
- The MCP initialize/tools-list test passed again: Harness 0.5.7, nine tools.
- Started **only Codex**, with explicit Codex caller identity, in a separate
  Harness scratch project and with `--mode ask`. No sandbox/approval bypass.

```bash
harness new codex ruti-bootstrap-smoke-20261029 --mode ask --name ruti-bootstrap-smoke -- 'Reply exactly RUTI_OPENHARNESS_CODEX_OK. Do not call tools, edit files, start agents, or modify configuration. This is a subscription-only bootstrap smoke test.'
harness wait ruti-bootstrap-smoke --timeout 30s --json
harness show ruti-bootstrap-smoke --chars 5000 --json
```

Actual evidence: harness ID `bc979604-96d5-4c2c-a34b-2b8eca3dcfb7`;
`wait` returned `idle`; `show` returned exactly `RUTI_OPENHARNESS_CODEX_OK`,
`tools=0`, `truncated=false`, `startedBy=you`. This verifies an actual Codex
subscription turn through Harness. The smoke harness was then closed with
`harness close ruti-bootstrap-smoke --json`; its history and scratch folder
were retained. This was a **Codex → Codex** bootstrap check, not the stage B
**Claude-manager → Codex-child → independent review** acceptance test.

## Ruti and Windows regression checks

All real-profile Ruti commands used `RUTI_HOST=codex` and the stable session ID
`01a18753-5f35-7133-bc6a-5ca478137ba5`.

| Executed command/check | Actual result |
|---|---|
| `ruti --help` | CLI available |
| `ruti install --host codex` | Preview: everything already installed and current; no apply required |
| `ruti mode --json` | Codex-scoped session; coding/jev/wait/flow/manager/timemachine on, council auto, free off, effplus off |
| `ruti defaults --json` | Defaults and overrides read; not changed |
| `ruti status --json` | Codex budget UNKNOWN, percentages null, freshness never; proxy alive, 22 models, LM Studio up with no resident model |
| `ruti doctor --json` (no fix) | WARN: registered Claude statusline has no quota reading; other checks ok. Jev result was explicitly cached from 33 minutes earlier, not a new model probe |
| `ruti report --host codex --json` | Existing historical ledger: 4 accepted, 4 rejected, 2 unreviewed; subscription savings null; not results of this pilot |
| `py -3.12 -m compileall -q ruti` | PASS, exit 0 |
| Windows isolated pytest below | PASS, 47 tests in 2.24 seconds, exit 0 |
| Windows config preservation | SHA256 unchanged for `.claude/settings.json`, `.claude/CLAUDE.md`, `.codex/config.toml`, `.codex/AGENTS.md`, `.config/opencode/opencode.json` |
| Final native auth checks | Claude still claude.ai Pro; Codex still ChatGPT |

Windows tests used a temporary `RUTI_HOME`, not the live state:

```powershell
py -3.12 -m pytest tests/test_codex_host.py tests/test_flow_session.py tests/test_session_start_hook.py -q --junitxml=build/openharness-bootstrap/windows-pytest.xml
```

Sanitized evidence: [WINDOWS_PYTEST.xml](WINDOWS_PYTEST.xml). These are unit tests,
with mocks/fixtures where defined in the suite; they do not demonstrate live
OpenHarness orchestration or automatic Codex support for Claude hooks.
The full test suite, Linux tests, CI matrix and visual statusline comparison
were **NOT RUN** in stage A.

Existing Windows Claude statusLine and seven hook event groups remain configured;
existing plugins/integrations were preserved. Claude inference was deliberately
not used as a regression probe because the user prohibits launching it as a child.

Linux Ruti is not installed yet. Its current Python lacks pytest, click, rich,
httpx and psutil. Stage B needs an isolated Linux environment for Linux tests;
Windows hooks, scheduled tasks, GPU/proxy access and state must be tested separately.
Do not silently share Windows `RUTI_HOME` with Linux. Network reachability to the
Windows proxy/GPU from Linux is **NOT VERIFIED** and no proxy endpoints were changed.

An `OPENAI_API_KEY` exists in the Windows environment, but Codex reports ChatGPT
authentication. Its value was never printed or used. In the checked Linux shell,
OPENAI_API_KEY, ANTHROPIC_API_KEY and both provider BASE_URL variables were absent.
Initial installation and MCP checks made no model request. After user sign-in,
one live Codex smoke turn used ChatGPT authentication; no API-key model request
was issued. No subscription savings or account-wide zero-charge claim is made.

## User action and handoff

Follow [CLAUDE_MANAGER.md](CLAUDE_MANAGER.md) in an independent user terminal.
Linux Claude Pro, Codex ChatGPT authentication, Harness account consent, MCP,
and a live Codex launch/wait/show/close cycle are now verified. Windows
subscriptions were preserved; Linux CLI sign-ins were performed by the user.

Stage A bootstrap is ready for handoff. The actual independent Claude-manager
and its Codex-child review loop belong to stage B and remain unverified.
HACCP rollout is **NO-GO / not assessed beyond bootstrap prerequisites**.
No stage B implementation or final HACCP readiness assessment was performed.

## Official sources consulted

- [OpenHarness CLI platforms and installation](https://github.com/autonomous-ai/openharness/blob/main/cli/README.md)
- [OpenHarness agent commands and user consent](https://harness.autonomous.ai/agents)
- [Harness skill and nesting rules](https://github.com/autonomous-ai/openharness/blob/main/plugins/harness/skills/harness/SKILL.md)
- [Harness plugin installation and MCP](https://github.com/autonomous-ai/openharness/blob/main/plugins/harness/README.md)
- [Issue 105: UI/backend divergence](https://github.com/autonomous-ai/openharness/issues/105)
- [Issue 110: Windows Codex interop runner failure](https://github.com/autonomous-ai/openharness/issues/110)
- [Official OpenAI CLI installation](https://learn.chatgpt.com/docs/cli)
- [Official OpenAI authentication](https://learn.chatgpt.com/docs/auth)
- [Official Claude Code setup](https://code.claude.com/docs/en/setup)
- [Official Claude Code authentication](https://code.claude.com/docs/en/authentication)

Both cited Harness issues were closed as completed when read. Issue 110's
maintainer specifically points to Windows Codex reached via WSL interop, not
proof of a general Linux Harness defect. Native Linux ELF verification addresses
that specific setup risk; it does not prove full WSL reliability.
The Ruti documentation/CI GitHub URLs could not be fetched by the web tool;
the actual local `docs/codex.md`, `.github/workflows/ci.yml`, installer and
diagnostic source were inspected instead. No remote CI pass is claimed.
