# Запрос для сессии Ruti (C:\mycode\ruti) — 23.11.2026

## Проблема
Служба `ruti-operator-approver` отвечает на запросы подтверждения Codex по allowlist. Исполнители Codex (например codex-jcr2, codex-rv789/791/792) при ревью PR читают комментарии к PR командой вида:
  gh api repos/uausername/haccp/pulls/N/comments [--paginate] [--jq ...] [-q ...] [-H ...]
Allowlist такую форму не знает, поэтому каждый раз Codex останавливается и просит подтверждение. Codex переформулирует команду с другими флагами (--paginate, --jq, --method GET, per_page=...) — и каждый вариант снова не проходит. Итог: исполнитель стоит, пока кто-то не нажмёт «да» вручную; мастер и Администратор тратят время и квоту.

## Что нужно
Разрешить в allowlist approver'а ЧТЕНИЕ комментариев PR через gh api, независимо от набора флагов:
- Разрешать: `gh api` с явным или неявным методом GET, путь `repos/<owner>/<repo>/pulls/<N>/comments`, а также `.../pulls/<N>/reviews`, `.../pulls/<N>/reviews/<id>/comments`, `.../issues/<N>/comments` (только чтение), с любыми из флагов: `--paginate`, `--jq/-q`, `--method GET/-X GET`, `-H` (Accept), `-f/-F per_page=..`, `--slurp`, `--cache`.
- Репозиторий ограничить: uausername/haccp (и репозитории проекта, если есть).
- НЕ разрешать: методы POST/PATCH/PUT/DELETE, `-f/-F` с телом (кроме per_page/page), `--input`, любые пути вне pulls/issues comments/reviews, цепочки команд (`;`, `&&`, `|` в сторону записи, подстановки `$(...)`).
- Лучше разбирать команду токенами (shlex), а не regex по строке: тогда порядок флагов не имеет значения.

## Как проверить
1. Из папки worktree Codex выполнить `gh api repos/uausername/haccp/pulls/792/comments --paginate --jq '.[].body'` — approver отвечает «да» без участия человека.
2. Вариант с `--method POST` или `-f body=...` — отклоняется/остаётся человеку.
3. Записать решение в журнал approver'а как обычно.

## Связанное (раньше просили)
- `ruti off` не в allowlist approver'а (17.11) — добавить.
- north-mini-code удалена из Ruti, прокси не перезапущен (warning в health check: declared/served расходятся) — перезапустить прокси.
- Задача RutiLiteLLM не зарегистрирована, `lms` не установлен — только предупреждения.
