# StopFailure hook

`on-pro-limit.ps1` pops a Windows dialog when Claude Code's turn fails on a rate limit or billing
error, reminding you the delegates are still available while the 5-hour window resets.

The hook is fire-and-forget: it cannot intercept the failed turn or continue it on another model —
Claude Code gives hooks no way to do that. It only tells you what happened.

Register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "StopFailure": [
      {
        "matcher": "rate_limit|billing_error",
        "hooks": [
          {
            "type": "command",
            "command": "powershell.exe",
            "args": ["-NoProfile", "-File", "C:\\mycode\\ruti\\hooks\\on-pro-limit.ps1"],
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

The script reads the hook payload (`error_type`, `error_message`) as JSON on stdin. To test it
without waiting to hit a real limit:

```powershell
'{"error_type":"rate_limit","error_message":"test"}' | powershell -NoProfile -File hooks\on-pro-limit.ps1
```
