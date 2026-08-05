$ErrorActionPreference = 'SilentlyContinue'
$stdin = [Console]::In.ReadToEnd()

$errorType = 'unknown'
$errorMessage = ''
try {
    $payload = $stdin | ConvertFrom-Json
    if ($payload.error_type) { $errorType = $payload.error_type }
    if ($payload.error_message) { $errorMessage = $payload.error_message }
} catch {}

$text = "Claude Code hit a Pro-subscription limit ($errorType).`nOpen a terminal and use 'opencode run ...' or 'qwen -p ...' to keep working locally/via Gemini until the 5-hour window resets."

$wshell = New-Object -ComObject Wscript.Shell
$wshell.Popup($text, 0, "ruti: Claude Code limit hit", 0x30) | Out-Null
