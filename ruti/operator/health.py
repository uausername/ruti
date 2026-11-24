"""Login watchdog: tell the person when Claude or Codex in WSL is logged out.

On 2026-11-22 the WSL Claude login expired; the master and the Administrator share it,
so both stopped on "Not logged in" for almost two days and nobody was told -- the
Administrator who would have said so was logged out too, and the services did not look.

The waker calls :meth:`Watch.tick` every 5 minutes. A check that turns bad shows a
Windows notification (WSL interop, ``powershell.exe``) and writes ``needs_admin``; while
it stays bad the notification repeats every ``repeat_s`` (2 h); when it recovers, one more
notification says so. State: ``$STATE/operator/health.json``; log lines in ``waker.jsonl``.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str]], subprocess.CompletedProcess]
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
# Windows PowerShell's own AppUserModelID: toasts from an unregistered id are dropped on Windows 11.
PS_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


def _exe(name: str) -> str:
    return shutil.which(name) or str(Path.home() / ".local/bin" / name)


def check_claude(run: Runner = _run) -> tuple[bool, str]:
    try:
        done = run([_exe("claude"), "auth", "status"])
        data = json.loads(done.stdout or "{}")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return False, f"`claude auth status` failed: {exc!r}"[:200]
    if data.get("loggedIn"):
        return True, "logged in"
    return False, "Claude в WSL не залогинен (authMethod=%s)" % data.get("authMethod")


def check_codex(run: Runner = _run) -> tuple[bool, str]:
    try:
        done = run([_exe("codex"), "login", "status"])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"`codex login status` failed: {exc!r}"[:200]
    text = (done.stdout + done.stderr).strip()
    if done.returncode == 0 and "logged in" in text.lower() and "not logged in" not in text.lower():
        return True, text[:120]
    return False, "Codex в WSL не залогинен: " + text[:120]


FIX = {
    "claude": "Открой ярлык «Администратор» и набери /login.",
    "codex": "В окне WSL выполни: codex login",
}


def toast_script(title: str, text: str) -> str:
    esc = lambda s: (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
                     .replace("'", "''"))
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('<toast scenario=\"reminder\"><visual><binding template=\"ToastGeneric\"><text>{esc(title)}</text>"
        f"<text>{esc(text)}</text></binding></visual><actions><action content=\"OK\" arguments=\"ok\" "
        "activationType=\"system\"/></actions></toast>');"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{PS_AUMID}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
    )


def notify_windows(title: str, text: str, run: Runner = _run) -> bool:
    if not Path(POWERSHELL).exists():
        return False
    enc = base64.b64encode(toast_script(title, text).encode("utf-16-le")).decode("ascii")
    try:
        return run([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Watch:
    def __init__(self, state: Path, log: Callable[..., dict], *, run: Runner = _run,
                 notify: Callable[[str, str], bool] | None = None, now: Callable[[], float] = time.time,
                 checks: dict[str, Callable[[], tuple[bool, str]]] | None = None, repeat_s: float = 7200):
        self.state, self.log, self.now, self.repeat_s = state, log, now, repeat_s
        self.notify = notify or (lambda t, x: notify_windows(t, x, run))
        self.checks = checks or {"claude": lambda: check_claude(run), "codex": lambda: check_codex(run)}

    def _load(self) -> dict:
        try:
            return json.loads(self.state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def tick(self) -> dict:
        st = self._load()
        now = self.now()
        for name, check in self.checks.items():
            ok, detail = check()
            prev = st.get(name) or {}
            if ok:
                if prev.get("ok") is False:
                    self.notify("Ruti: вход восстановлен", f"{name}: снова залогинен, агенты могут работать.")
                    self.log("health_recovered", check=name)
                st[name] = {"ok": True, "since": prev.get("since") if prev.get("ok") else now}
                continue
            first = prev.get("ok") is not False
            due = first or now - float(prev.get("notified_at") or 0) >= self.repeat_s
            entry = {"ok": False, "since": now if first else prev.get("since", now), "detail": detail,
                     "notified_at": prev.get("notified_at")}
            if due:
                hours = (now - float(entry["since"])) / 3600
                text = detail + (f" (уже {hours:.0f} ч)" if hours >= 1 else "") + ". " + FIX.get(name, "")
                shown = self.notify("Ruti: агенты стоят — нужен вход", text)
                entry["notified_at"] = now
                self.log("needs_admin", reason=detail + ". " + FIX.get(name, ""), check=name, toast=shown)
            st[name] = entry
        tmp = self.state.with_suffix(".tmp")
        self.state.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state)
        return st
