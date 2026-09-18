"""Start the LiteLLM proxy with no console window -- what the RutiLiteLLM task runs.

Run by `pythonw.exe`, which has no console of its own, and it starts litellm with
CREATE_NO_WINDOW, so no console is ever created that could be shown. That is the
point of this file. `powershell.exe -WindowStyle Hidden` stopped being enough once
the task no longer ran elevated: on Windows 11 the default terminal is Windows
Terminal, which takes over the console of an ordinary (non-elevated) process and
cannot hide it, so every logon left an empty window whose closing killed the proxy.

It does what start-litellm.ps1 does -- load .env into the environment, stub any
missing generated include, bind to loopback only -- and that script stays for a
foreground start by hand. Nothing here is encoded or hidden from inspection: the
last attempt to launch the proxy through an encoded PowerShell command got
powershell.exe flagged by the antivirus.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERATED = ("models.generated.yaml", "providers.generated.yaml")
# LiteLLM defaults to 0.0.0.0 and serves /model/info without auth; see start-litellm.ps1.
BIND_HOST = "127.0.0.1"


def load_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, parsed with the same pattern as start-litellm.ps1."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        match = re.match(r"^\s*([^#=]+?)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def litellm_executable() -> str:
    found = shutil.which("litellm")
    if found:
        return found
    # pythonw.exe lives next to python.exe; its console scripts are in Scripts\.
    return str(Path(sys.executable).parent / "Scripts" / "litellm.exe")


def command(here: Path = HERE) -> list[str]:
    return [litellm_executable(), "--config", str(here / "config.yaml"), "--host", BIND_HOST]


def main() -> int:
    env = {**os.environ, **load_env(HERE / ".env"), "PYTHONIOENCODING": "utf-8"}
    for name in GENERATED:
        stub = HERE / name
        if not stub.exists():
            stub.write_text("# Placeholder until ruti regenerates it.\nmodel_list: []\n",
                            encoding="utf-8")
    with (HERE / "litellm.log").open("ab") as log:
        return subprocess.call(
            command(), cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )


if __name__ == "__main__":
    sys.exit(main())
