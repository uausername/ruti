# User launch and stage B handoff

This is the original stage A handoff. The stage B manager/child loop subsequently
ran; the latest implementation and validation are in [STAGE_C_REPORT.md](STAGE_C_REPORT.md).

Current state: stage A installation, Linux subscription logins, Harness account
consent, MCP and a live Codex smoke turn are verified. Start the manager yourself
in a separate terminal. Do not run it from a Codex-created child session.

## Linux authentication — already completed

From Windows PowerShell:

```powershell
wsl -d Debian
```

Then in the Debian shell:

```bash
export PATH="$HOME/.local/bin:$PATH"
claude auth login
codex login
harness login
harness settings agents-start-agents on
claude auth status --text
codex login status
harness whoami
```

Use the existing claude.ai Pro account and **Sign in with ChatGPT** for Codex.
Do not choose an API key or copy Windows auth files. Follow the browser/terminal
prompts locally; do not send credentials or login codes to an agent.
Do not enable Agent Messages for this basic new/wait/show workflow.

The user has completed the logins and consent above; Codex rechecked them.
Keep these commands as a recovery reference, not a requirement to log in again.
The bootstrap agent did not grant the Harness experimental permission itself.

## Launch independent Claude-manager

After authentication, from that same Debian user shell:

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /mnt/c/mycode/ruti
harness start
tmux new -s ruti-manager
claude
```

The last command is entered inside the newly opened tmux session. If that named
session already exists, inspect it rather than killing/replacing it:

```bash
tmux attach -t ruti-manager
```

In Claude paste:

> Прочитай целиком RUTI_OpenHarness_Codex_Claude_Pilot.md, затем
> docs/openharness-pilot/BASELINE.md и docs/openharness-pilot/REPORT.md.
> Ты — независимый top-level Claude-manager. Выполни этап B по документу.
> Сначала проверь собственную подписочную авторизацию, авторизацию Linux Codex,
> загрузку harness plugin/MCP и harness whoami из своей агентной сессии.
> Не начинай реализацию, пока запуск дочерних агентов не разрешён.
> Подготовь отдельный worktree/ветку для Codex и используй OpenHarness.
> Не запускай этот менеджер через harness new claude и не пытайся обходить
> ограничения вложенности, sandbox или approval. Учти незакоммиченные исходные
> изменения из BASELINE: worktree от HEAD их не содержит. Не сбрасывай их.
> Linux Ruti/pytest ещё не установлены: используй отдельное Linux venv, не переноси
> Windows state/auth/hooks и проверяй Windows-части отдельно. Не меняй глобальные
> конфигурации без backup. Не используй платные API вместо подписок.
> Реально выполни wait/show, независимый review и тесты. Подготовь отчёт и
> HACCP GO/NO-GO по документу; не пушь, не мержи и не меняй HACCP.

Fresh authenticated Codex sessions still need explicit `/plugins` visual
verification. A Codex → Codex smoke launch/wait/show succeeded, but independent
manager creation and the Claude-manager → child → review loop remain untested.
The 47 Windows unit tests in stage A are not evidence for that live loop.
