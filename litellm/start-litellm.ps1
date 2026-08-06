# Launches the LiteLLM proxy with keys from .env. Used by the "RutiLiteLLM" scheduled task
# (runs at logon) and can be run by hand for a foreground start.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# .env holds the Gemini keys and is gitignored - see ../.gitignore
Get-Content (Join-Path $here '.env') | ForEach-Object {
    if ($_ -match '^\s*([^#=]+?)\s*=\s*(.*?)\s*$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}

# config.yaml pulls in the generated model lists, and LiteLLM refuses to start if an
# included file is missing. Both are machine-specific and therefore gitignored, so on a
# fresh clone they do not exist yet - stub them rather than fail. `ruti models sync` and
# `ruti provider add` fill them in.
foreach ($name in @('models.generated.yaml', 'providers.generated.yaml')) {
    $generated = Join-Path $here $name
    if (-not (Test-Path $generated)) {
        Set-Content -Path $generated -Encoding utf8 -Value @(
            "# Placeholder until ruti regenerates it."
            'model_list: []'
        )
    }
}

# LiteLLM's startup banner is non-ASCII and crashes on the cp1251 console codepage
$env:PYTHONIOENCODING = 'utf-8'

Set-Location $here

# Bind to loopback only. LiteLLM defaults to 0.0.0.0 and serves /model/info without
# auth, so on any shared network the whole model list - and the ability to spend your
# API keys - is reachable by anyone who can route to this machine.
$bindHost = '127.0.0.1'

# litellm writes normal progress to stderr, so run it through cmd - PowerShell would
# otherwise treat that output as a terminating error.
& cmd.exe /c "litellm --config `"$here\config.yaml`" --host $bindHost >> `"$here\litellm.log`" 2>&1"
