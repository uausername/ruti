# Ruti × OpenHarness — инструкция для Codex (bootstrap) и Claude Code (manager)

Дата подготовки: 29 октября 2026. Целевой репозиторий: https://github.com/uausername/ruti . Будущий проект: https://github.com/uausername/haccp .

## Задача и ограничения

Нужно испытать OpenHarness как инфраструктуру совместной работы двух установленных CLI по подпискам: Claude Code (управляющий) и Codex CLI (исполнитель/независимый ревьюер). Не использовать API-ключи Anthropic/OpenAI вместо подписок. Не переподключать OAuth к чужим proxy, не менять `ANTHROPIC_BASE_URL`/`OPENAI_BASE_URL`, не создавать платные API-вызовы без отдельного разрешения. Ruti уже использует отдельные провайдеры, LiteLLM/OpenCode и Jev; не удалять и не ломать их существующую конфигурацию.

**Два этапа:** Codex, которому передадут этот файл, делает bootstrap, установку, диагностику и готовит рабочее пространство. После проверки пользователь запускает Claude Code самостоятельно, как **top-level session** (не через `harness new claude` из Codex). Claude делегирует реализацию и тесты дочернему Codex через OpenHarness. Не выдавать настройку Codex за успешную работу управляющего Claude.

## Проверенные источники (читать перед действиями)

1. OpenHarness repo: https://github.com/autonomous-ai/openharness
2. OpenHarness install/platform: https://github.com/autonomous-ai/openharness/blob/main/cli/README.md
3. Официальные команды для AI-агентов: https://harness.autonomous.ai/agents
4. OpenHarness skill и ограничения: https://github.com/autonomous-ai/openharness/blob/main/plugins/harness/skills/harness/SKILL.md
5. Плагин и установка для Claude/Codex: https://github.com/autonomous-ai/openharness/blob/main/plugins/harness/README.md
6. Ruti: https://github.com/uausername/ruti
7. Ruti Codex: https://github.com/uausername/ruti/blob/main/docs/codex.md
8. Ruti CI: https://github.com/uausername/ruti/blob/main/.github/workflows/ci.yml
9. Ruti statusline implementation: https://github.com/uausername/ruti/blob/main/ruti/statusline.py
10. Ruti modes: https://github.com/uausername/ruti/blob/main/ruti/modes.py
11. Ruti Codex installer: https://github.com/uausername/ruti/blob/main/ruti/install.py
12. Codex native TUI status-line config: https://github.com/openai/codex/blob/main/codex-rs/config/src/types.rs
13. Запрос на внешнюю команду для кастомного status line Codex: https://github.com/openai/codex/issues/20043
14. Известные проблемы OpenHarness на Windows/WSL2: https://github.com/autonomous-ai/openharness/issues/105 и https://github.com/autonomous-ai/openharness/issues/110

## Этап A. Codex CLI: bootstrap и проверка окружения

### A1. Проверки до установки

1. Убедиться, что открыт **правильный локальный checkout** `uausername/ruti`: `git remote -v`, `git status --short --branch`, `git rev-parse HEAD`. Зафиксировать текущие HEAD, ветку и грязные файлы **сначала во временном файле вне репозитория**, а после создания пилотной ветки перенести в `docs/openharness-pilot/BASELINE.md`; не включать секреты и пути с персональными данными. Не сбрасывать и не stash-ить пользовательские изменения без разрешения.
2. Определить ОС и оболочку (`uname -a`, `command -v` на macOS/Linux; `$PSVersionTable`, `Get-Command` в PowerShell), платформу и путь, из которых реально запускаются подписочные `claude` и `codex`. Проверить `claude --version`, `codex --version`, Python 3.12+, `git`, `tmux`, `node` и `ruti --help`; не выводить токены и содержимое auth-файлов.
3. **Стоп-условие:** официальный OpenHarness CLI поддерживает macOS/Linux, не native Windows. Если Ruti и оба CLI живут только в Windows PowerShell, не выдавать такую установку за рабочую. Проверить WSL2 отдельно; для реального управления OpenHarness в WSL должны существовать именно Linux-доступные `claude`/`codex` с действующими *своими* логинами. Не переносить автоматически auth.json, cookies или Claude OAuth из Windows в WSL. Предложить пользователю отдельно авторизовать Linux CLI. Не предпринимать установку WSL2/миграцию проекта без подтверждения.
4. Если WSL2 используется, сначала оценить совместимость Windows-ориентированного Ruti (Windows Terminal, PowerShell hooks, `%LOCALAPPDATA%`, LM Studio/LM Studio proxy и GPU) с Linux-сессиями. Провести нативные Windows unit/CLI тесты отдельно. Известны сбои OpenHarness WSL2; отметить это как эксперимент, а не подтверждённую поддержку Windows.

### A2. Установка OpenHarness на поддерживаемой платформе

Официальные шаги для macOS/Linux (перед выполнением удалённого installer-скрипта ознакомиться с его содержимым):

```bash
curl -fsSL https://harness.autonomous.ai/cli/install.sh -o /tmp/openharness-install.sh
# Ознакомиться с /tmp/openharness-install.sh; затем:
bash /tmp/openharness-install.sh
harness version
harness start
harness status
harness whoami
```

Для того чтобы AI-агенты могли запускать других агентов, пользователь должен войти в Harness и включить экспериментальное разрешение. Это действие требует участия пользователя:

```bash
harness login
harness settings agents-start-agents on
harness whoami
```

Не утверждать, что доступ активирован, пока `harness whoami` этого не подтвердит. Agent Messages **не требуется** для базового сценария `new/wait/show`; если потребуется обмен сообщениями — включать отдельно с разрешения пользователя: `harness settings agent-messages on`.

Установить Harness plugin для **обоих** CLI по официальной документации:

```bash
claude plugin marketplace add autonomous-ai/openharness --sparse .claude-plugin plugins
claude plugin install harness@openharness
codex plugin marketplace add autonomous-ai/openharness --sparse .claude-plugin --sparse plugins
codex plugin add harness@openharness
```

Перезапустить сессии CLI, чтобы загрузить plugin. Если версия CLI не поддерживает команды plugin — не предполагать, что установка удалась; воспользоваться официальной fallback-инструкцией skill или описать блокер. Проверить MCP/skill через доступные команды CLI и `harness help agents`, `harness ps`; записать точные версии/результаты. Для Codex plugin проверять в новой сессии (`/plugins`). Никогда не включать режимы отключения sandbox/approval (`--dangerously-*`, `--mode full`) ради успеха теста.

### A3. Ruti и исходное состояние

- Проверить `ruti install --host codex` **сначала без `--apply`**, изучить diff. Применять только после сохранения backup `~/.codex/AGENTS.md`, не затерев пользовательские инструкции. `ruti install --host codex --apply` ставит guidance, **не** устанавливает Claude hooks или его statusLine.
- Проверить `ruti mode --json`, `ruti status --json`, `ruti doctor`, `ruti defaults --json`, `ruti report --host codex --json` в релевантном окружении. У Codex нужен `RUTI_HOST=codex` и актуальный `CODEX_THREAD_ID` или уникальный для *этой* сессии `RUTI_SESSION_ID` (не общий константный ID). Зафиксировать отсутствие данных, не подставлять метрики Claude за Codex.
- Сохранить без секретов базовый перечень текущих настроек, установленных интеграций и скриптов; проверить, что Claude Code продолжает работать как прежде.
- На этом завершить bootstrap, сообщить пользователю команды для top-level запуска Claude. **НЕ запускать `harness new claude` из Codex ради создания менеджера:** агент, созданный другим агентом, по правилам Harness не может сам запускать harnesses. Это нарушит требуемую архитектуру.

## Этап B. Человек запускает Claude top-level, Claude управляет Codex

На поддерживаемой платформе в отдельном терминале пользователя, из директории Ruti:

```bash
cd /PATH/TO/ruti
harness start
tmux new -s ruti-manager
claude
```

Harness обнаруживает CLI в tmux, и Claude с установленным plugin может вызывать `harness whoami`, `harness ps`, `harness new codex`, `harness wait`, `harness show`, `harness close`. Создавать/удалять worktrees только по безопасному Git-процессу. Без необходимости не начинать несколько агентов на одном checkout. Работать на отдельной ветке от исходного HEAD; не пушить и не мерджить без разрешения пользователя.

### Текст задания для top-level Claude

> Ты — управляющий пилотным проектом Ruti × OpenHarness. Работай в `uausername/ruti`. Оцени baseline, уточни текущий план по реальному коду и создай отдельный Git worktree/ветку для Codex (не дели один рабочий каталог на одновременное редактирование). Делегируй реализацию status line и необходимые тесты Codex через OpenHarness (`harness new codex <worktree-path> --name ruti-statusline -- "..."`). Укажи пути, критерии готовности и что Codex не должен пушить/мерджить/деплоить. Продолжай собственной независимой проверкой архитектуры, тестов и требований. Дождись завершения через `harness wait`, прочитай `harness show`, проверь diff, локально воспроизведи тесты, запроси не больше двух циклов исправлений (новый harness, если Agent Messages выключены). Не утверждай, что испытание завершено, если платформа, авторизация, реальные тесты или визуальная проверка не подтверждены. Подготовь отчёт по правилам ниже и рекомендации для HACCP. Не создавай billable API-запросов без согласия. Не меняй подписочный OAuth, глобальные профили или продуктивные ветки.

**Критически важно:** не запускать Claude как подагента из Codex; иначе Claude не сможет создать Codex-подагента по ограничению Harness. Для обмена итогами достаточно `wait/show`; Agent Messages опциональны и выключены по умолчанию.

## Техническое задание Ruti: Codex status line

**Цель:** максимально близкое к Claude CLI представление статусной строки Ruti, но с корректными *Codex-данными*, в терминале, где работает Codex, не меняя подписочную авторизацию. Изучить `ruti/statusline.py`, `ruti/quota.py`, `ruti/modes.py`, `ruti/install.py`, `docs/codex.md` и существующие tests. Прежде чем писать код, явно зафиксировать ограничения Codex CLI: штатный `[tui].status_line` использует конечный список встроенных элементов и **не** запускает произвольный скрипт `statusLine.command` как Claude. Не имитировать неподдерживаемые функции и не объявлять полную идентичность, если доказать её невозможно.

1. Проверить текущую версию Codex CLI и её `/statusline`; собрать доступные элементы. Никаких выдуманных имен. Не затирать существующий `~/.codex/config.toml` — сохранять точечный дифф и backup.
2. Рассмотреть два подхода: **native footer** (штатные данные Codex, но ограниченная кастомизация) и **external terminal/tmux status view** для собственных сегментов Ruti. Если OpenHarness не отображает tmux status bar, это должно быть проверено на реальном UI и указано в отчёте. Возможен внешний параллельный монитор, но это не статусная строка Codex и не полный успех требования. Не патчить бинарник Codex и не переписывать глобальный `~/.tmux.conf` без согласования.
3. Добиться максимально возможного визуального соответствия: порядок, цвета, размеры, 1/2/3 строки, узкий/широкий терминал, отсутствие ANSI-мусора. Вынести общий рендеринг/сегменты в переиспользуемые и тестируемые функции; не связывать Codex с Claude-only stdin JSON.
4. Показать *Codex*-модель и reasoning, окно 5 часов и неделю только если реальные Codex-метрики доступны. Отмечать неизвестное `unknown`, устаревшее `stale` и отсутствие данных; не читать Claude quota и не выдумывать цифры. Режимы `coding`, `free`, `council`, `jev`, `wait`, `flow`, `manager`, `timemachine`, `eff+`, kill switch OFF и диагностические признаки должны отражать *текущую Codex-сессию*, а не общую глобальную сессию. Если режим есть как CLI-флаг, но требует Claude hooks, статус может показывать конфигурацию, **но** должен отдельно сообщать, что автоматическое поведение не поддержано.
5. Тестировать независимость сессий Claude/Codex и нескольких Codex session IDs, фоновый обновлятор, время обновления и задержки. Не допускать блокирования TUI из-за сети, прокси или Ruti doctor. Проверить, что Claude statusLine и hooks после изменений не регрессировали.
6. Зафиксировать выбор архитектуры и демонстрацию (терминальный capture/скриншот по возможности, без секретов). Если точная вставка кастомного текста внутрь встроенного Codex footer невозможна, честно указать `PARTIAL` / `BLOCKED BY CODEX TUI`, предложить реальный обходной путь и не писать `PASS`.

## Матрица проверок всех режимов Ruti

Не ограничиваться README: автоматически сопоставить CLI (`ruti mode --help` и `ruti show --help`), исходники и тесты; текущий перечень в `ruti/cli.py`:

| Функция | Проверки | Важное ограничение |
|---|---|---|
| `ruti off/on` | Раздельность сессий; `route/delegate` блокируются и восстанавливаются; OFF видим | Уникальный Codex session id |
| `ruti mode coding on/off` | Подсказка/маршрутизация, status | Кодекс не имеет Claude UserPromptSubmit hook |
| `ruti mode free off/soft/hard` | Рейтинг и запрет billable delegate в hard, отсутствие обхода | В тестах mock/fixture, без API-расходов |
| `ruti mode council off/on/auto` | Предикаты, судья, недостаток ключа | API/классификатор опционален; не вызывать платные модели |
| `ruti mode wait on/off/pause` | Уровни 90/95%, checkpoint/resume | Поведение зависит от Claude Pre/PostToolUse/Stop hooks — в Codex не считать поддержанным автоматически |
| `ruti mode flow on/off` и `ruti flow handoff` | Передача контекста, 50/60%, название, chain cap | Запуск Windows Terminal/Claude из Claude Stop hook — не Codex-native |
| `ruti mode manager on/off` | Рекомендации модели/effort, жизненный цикл | Изменение Claude seat — не изменение Codex модели |
| `ruti mode eff+ on/off` | Усиление effort при разрешённой квоте | Зависит от Claude seat recommendation |
| `ruti mode timemachine on/off` | Brief, letters, предупреждения, native hook | Hook эффекты Claude-only без адаптации |
| `ruti mode jev on/off` | Отсутствие внешнего запроса в off; on лишь с ключом | Сторонний API, нужен stub для теста |
| `ruti defaults set/show/clear` | Наследование, session overrides, чистота | Не менять глобальные defaults при тестировании на живом профиле |
| `ruti show all/2/1/none/toggle`, `ruti hide` | Число строк, выключение/включение рендера | Подтвердить на реальном terminal/TUI |
| `ruti route`, `ruti delegate`, `ruti verdict`, `ruti worktree apply` | Guardrails, attribution, timeout, изменение файлов и rollback safety | Для тестов использовать изолированные repos/mocks; платные модели не дергать |
| `ruti status`, `ruti doctor`, `ruti report --host codex` | Отсутствующие данные, корректная диагностика и атрибуция, без ложной экономии | Пробные команды без auto-fix |

Каждый пункт: Unit / Integration / Live; фактический результат PASS, FAIL, PARTIAL, BLOCKED, NOT RUN; evidence (команда, лог или тест), отмечать real vs mock. Для wait/flow/manager/timemachine не притворяться, что наличие режима означает автоматическую поддержку Codex.

## Тестирование и отчёт

1. Создать изолированную test state (`RUTI_HOME` в tempdir), мокать quotas, сеть, proxy, LM Studio и clocks; не трогать настоящие режимы, tokens, настройки и файлы пользователя. Проверить Unix epoch `rate_limits.*.resets_at`, Windows shell/UTF-8/ANSI, stale values, смену контекста и сессий.
2. У Ruti CI используется **Windows Python 3.12, Windows Python 3.13 и Ubuntu Python 3.12**. Локально, в поддерживаемой среде, запустить `python -m compileall -q ruti`, `python -m pytest -q --junitxml=...`; отдельно проверить нативную Windows интеграцию. Если зависимости не установлены — отчёт о факте, а не выдуманный PASS.
3. Live-проверка подписок: Claude управляющий действительно создал отдельный Codex harness, `harness wait` вернул корректное состояние, `harness show` дал результат, Claude самостоятельно обнаружил/проверил diff и тесты. Проверить ограничение 4 живых дочерних harnesses и отсутствие попыток nested spawn. Если для теста требуется login/approval, получить его от пользователя и не обходить safety mechanisms.
4. Проверить после изменений `ruti install` (только preview), `ruti install --host codex` (preview), и `ruti doctor` без `--fix`; README и docs/codex.md обновлять только если поведение действительно изменилось.
5. Проверить отсутствие потерь при ошибках: агент остановлен, квота исчерпана, timeout, needs user, CLI умер, разорвана связь UI, worktree конфликтует, stale/invalid status data. Восстановление и неизменность `main` важнее успешного демонстрационного вывода.
6. Результаты разместить в отдельной ветке и создать `docs/openharness-pilot/REPORT.md` (платформа, версии, схема, список команд и их выводов, реальная матрица режимов, доказательство UI, тесты, ошибки и ограничения, затраты/квоты лишь если измерены, замечания независимого Claude review). Если возможно, добавить `docs/openharness-pilot/TEST_MATRIX.md`, `docs/openharness-pilot/HACCP_READINESS.md` и машинный `pytest-results.xml` (без приватных логов). Не включать секреты в git.
7. HACCP readiness: явные критерии **GO/NO-GO**, включая реальное подключение двух подписочных CLI, top-level Claude управление, отдельные worktrees, живые tests/review/repair, целостность состояния, отсутствие API charges, поведение Windows/WSL и необходимость человека для approvals. Исходный HACCP не изменять. Дальнейшее внедрение — отдельная задача после принятия отчёта.
8. Если нет возможности выполнить этап на текущей ОС, подготовить **честный blocker report**, а все unit/функциональные работы, не зависящие от OpenHarness, проводить отдельно. Не утверждать, что end-to-end verified.

## Критерии готовности

- Базовые Claude Code и Codex CLI продолжают работать по собственным подпискам без ключей API.
- Настоящий независимый Claude-manager (не подагент Codex) создал и проверил Codex-child через `harness new/wait/show`.
- Нет риска порчи `main`, глобального `AGENTS.md`, `config.toml` или секретов.
- Совпадение статуса Ruti на доступном уровне доказано скриншотом/terminal capture; недоступная кастомизация внутри Codex TUI явно отмечена.
- Все режимы перечислены и проверены по матрице; Claude-only части названы отдельно.
- Тесты/CI с реальными логами, нет вымышленных PASS.
- HACCP имеет отдельное решение GO/NO-GO, а не безусловную рекомендацию.

**Финальный ответ Codex после bootstrap:** что установлено и проверено; что требует действий пользователя (Harness login/Experimental, Linux CLI auth, старт Claude); точная команда для запуска топ-уровневого Claude; что заблокировано; ни одного выдуманного результата теста. После этого Codex останавливается и передаёт управление пользователю/Claude.
