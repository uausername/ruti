# Launch the installed Ruti-enabled Codex even in shells with an older PATH.
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CodexArguments)

$rutiCodex = Join-Path $env:LOCALAPPDATA 'ruti/codex-cli/0.164.0-ruti/codex.exe'
if (-not (Test-Path -LiteralPath $rutiCodex -PathType Leaf)) {
    throw 'The Ruti Codex build is not installed. See docs/codex.md for build/install instructions.'
}
& $rutiCodex @CodexArguments
exit $LASTEXITCODE
