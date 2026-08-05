# Launches the LiteLLM proxy with keys from .env. Used by the "RutiLiteLLM" scheduled task
# (runs at logon) and can be run by hand for a foreground start.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# .env holds the Gemini keys and is gitignored - see ../.gitignore
Get-Content (Join-Path $here '.env') | ForEach-Object {
    if ($_ -match '^\s*([^#=]+?)\s*=\s*(.*?)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}

# LiteLLM's startup banner is non-ASCII and crashes on the cp1251 console codepage
$env:PYTHONIOENCODING = 'utf-8'

Set-Location $here

# litellm writes normal progress to stderr, so run it through cmd - PowerShell would
# otherwise treat that output as a terminating error.
& cmd.exe /c "litellm --config `"$here\config.yaml`" >> `"$here\litellm.log`" 2>&1"
