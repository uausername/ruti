"""Health checks, and the fixes for the ones that are safe to automate.

Every check here exists because the corresponding failure is *silent*. A stopped LM
Studio, a dangling model alias, an unrepaired TLS chain -- none of them announce
themselves. They surface as a delegate that answers a little oddly, or a provider key
that appears to be rejected, and cost far more time to diagnose than to detect.

Fixes are opt-in (`--fix`) and each one names what it will change before doing it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import litellm_cfg, lmstudio, planner, tls
from .config import (
    CA_BUNDLE, LITELLM_ENV, LITELLM_GENERATED, LITELLM_START_SCRIPT, REPO_ROOT,
    SCHEDULED_TASK, load_dotenv,
)

OK, WARN, BAD = "ok", "warn", "bad"

LIVE_OPENCODE = Path.home() / ".config" / "opencode" / "opencode.json"
REPO_OPENCODE = REPO_ROOT / "opencode" / "opencode.json"


@dataclass
class Check:
    name: str
    status: str
    message: str
    detail: str = ""
    fix: Callable[[], str] | None = None
    fix_label: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def worst(self) -> str:
        if any(c.status == BAD for c in self.checks):
            return BAD
        if any(c.status == WARN for c in self.checks):
            return WARN
        return OK

    @property
    def fixable(self) -> list[Check]:
        return [c for c in self.checks if c.fix and c.status != OK]


# --------------------------------------------------------------------------- fixes


def _fix_tls() -> str:
    certifi_count, added = tls.build_bundle()
    _set_env_var("SSL_CERT_FILE", str(CA_BUNDLE))
    return (
        f"merged {certifi_count} certifi roots with {added} from the OS store into "
        f"{CA_BUNDLE}, and pointed SSL_CERT_FILE at it in .env "
        f"(restart the proxy for it to take effect)"
    )


def _set_env_var(key: str, value: str) -> None:
    """Add or replace a KEY=VALUE line in litellm/.env, leaving comments intact."""
    lines = LITELLM_ENV.read_text(encoding="utf-8-sig").splitlines() if LITELLM_ENV.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if "=" in line and not line.strip().startswith("#") and line.split("=", 1)[0].strip() == key:
            lines[index] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={value}")
    LITELLM_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _wait_for_liveliness(budget_s: float, *, interval_s: float = 2.0) -> bool:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if litellm_cfg.liveliness(timeout=2.0):
            return True
        time.sleep(interval_s)
    return litellm_cfg.liveliness(timeout=2.0)


def _fix_proxy_start() -> str:
    """Bring the proxy up, trying the quiet path before the interactive one.

    `schtasks /Run` reports exit code 0 whether or not the triggered instance
    ever actually launches the action -- on a machine where something blocks
    elevated token duplication for a plain user account, the instance sits in
    the Queued state forever and the exit code says nothing about that (found by
    checking the Task Scheduler operational log: every *other* elevated task on
    that machine ran as SYSTEM or through a signed Group trigger, never as a
    plain user account, which is what actually gave the game away). Liveliness
    is therefore the only trustworthy signal, and a direct elevated launch --
    the same one the "run this by hand" instructions describe -- is the fallback
    when the task does not deliver within a few seconds.
    """
    from . import proc

    proc.run(["schtasks", "/Run", "/TN", SCHEDULED_TASK], timeout=15.0)
    if _wait_for_liveliness(12.0):
        return f"started via the {SCHEDULED_TASK} scheduled task"

    # .env is only readable by SYSTEM/Administrators (see the secrets check
    # below), so this still needs a real elevated token -- it prompts UAC once
    # rather than running silently.
    subprocess.Popen(
        [
            "powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
            "Start-Process powershell -Verb RunAs -ArgumentList "
            "'-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass',"
            f"'-File','{LITELLM_START_SCRIPT}'",
        ],
        stdin=subprocess.DEVNULL,
    )
    if _wait_for_liveliness(20.0):
        return "the scheduled task never left Queued; launched directly instead (UAC approved)"
    return (
        "the scheduled task did not come up, and a direct elevated launch is "
        "pending -- approve the UAC prompt if one is waiting, then re-run `ruti doctor`"
    )


def _fix_lmstudio_server() -> str:
    lmstudio.start_server()
    return "started the LM Studio server on :1234"


def _fix_sync() -> str:
    entries = [
        litellm_cfg.local_entry(planner.identifier_for(m.key))
        for m in lmstudio.list_models()
        if m.kind == "llm"
    ]
    litellm_cfg.write_generated(entries)
    wired = litellm_cfg.wire_include()
    suffix = " and wired the include: line" if wired else ""
    return f"regenerated {len(entries)} local model entries{suffix} (restart the proxy)"


def _fix_opencode_drift() -> str:
    LIVE_OPENCODE.parent.mkdir(parents=True, exist_ok=True)
    if not LIVE_OPENCODE.exists():
        shutil.copyfile(REPO_OPENCODE, LIVE_OPENCODE)
        return f"copied {REPO_OPENCODE} over {LIVE_OPENCODE}"

    # Merge rather than overwrite: a locally registered provider (`ruti provider
    # add`, `ruti openrouter setup`) adds models to the live file that the repo
    # template never had and never will -- those are not drift, and a plain
    # copy used to delete them every time the repo picked up a new baseline
    # model. Only the repo's own models are ever added or refreshed; anything
    # live-only is left alone.
    live = json.loads(LIVE_OPENCODE.read_text(encoding="utf-8"))
    repo = json.loads(REPO_OPENCODE.read_text(encoding="utf-8"))
    live_models = live.setdefault("provider", {}).setdefault("ruti-router", {}).setdefault("models", {})
    repo_models = repo.get("provider", {}).get("ruti-router", {}).get("models") or {}
    added = [name for name in repo_models if name not in live_models]
    live_models.update(repo_models)
    LIVE_OPENCODE.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    return f"merged {len(added)} new model(s) from the repo into {LIVE_OPENCODE}"


# -------------------------------------------------------------------------- checks


def _check_tls() -> Check:
    if tls.bundle_works():
        env = load_dotenv()
        if env.get("SSL_CERT_FILE") != str(CA_BUNDLE):
            return Check(
                "tls", WARN,
                "a working CA bundle exists but .env does not point at it",
                detail="the proxy will still verify against certifi and fail",
                fix=_fix_tls, fix_label="write SSL_CERT_FILE into .env",
            )
        return Check("tls", OK, "certificate chain verifies through the merged bundle")

    found = tls.detect()
    if found.intercepted:
        return Check(
            "tls", BAD,
            f"TLS to {found.host} is intercepted by {found.issuer!r}",
            detail=(
                "its root is trusted by the OS but absent from certifi, so every "
                "provider call fails with CERTIFICATE_VERIFY_FAILED -- which is easy "
                "to mistake for a rejected API key"
            ),
            fix=_fix_tls, fix_label="build a merged CA bundle and point .env at it",
        )
    if not found.os_store_ok:
        return Check("tls", WARN, f"{found.host} is unreachable", detail="offline?")
    return Check("tls", OK, "certificate chain verifies against certifi")


def _check_proxy_bind() -> Check:
    from . import proc

    try:
        result = proc.run(["netstat", "-ano"], timeout=15.0)
    except (proc.ToolNotFound, proc.ToolTimeout):
        return Check("proxy-bind", WARN, "could not inspect listening sockets")

    for line in result.stdout.splitlines():
        if ":4000" in line and "LISTEN" in line.upper():
            local = line.split()[1] if len(line.split()) > 1 else ""
            if local.startswith("0.0.0.0") or local.startswith("[::]"):
                return Check(
                    "proxy-bind", BAD,
                    f"the proxy is listening on {local} -- every interface",
                    detail=(
                        "/model/info answers without authentication, so anyone who can "
                        "route to this machine can read the config and spend your API keys. "
                        "start-litellm.ps1 passes --host 127.0.0.1; this process predates it"
                    ),
                )
            return Check("proxy-bind", OK, f"listening on {local}")
    return Check("proxy-bind", WARN, "nothing is listening on :4000")


def _check_proxy_alive() -> Check:
    if litellm_cfg.liveliness():
        served = litellm_cfg.served_models()
        return Check("proxy", OK, f"alive, serving {len(served)} model(s)",
                     detail=", ".join(served))
    return Check(
        "proxy", BAD, "not responding on /health/liveliness",
        detail=(
            f"the {SCHEDULED_TASK} scheduled task should start it at logon; "
            "`--fix` retries that and falls back to a direct elevated launch "
            "(one UAC prompt) if the task never leaves the Queued state"
        ),
        fix=_fix_proxy_start, fix_label="start the proxy",
    )


def _check_lmstudio() -> Check:
    if not lmstudio.available():
        return Check("lmstudio", WARN, "the `lms` CLI is not installed",
                     detail="local models are unavailable; remote providers still work")
    if not lmstudio.server_running():
        return Check(
            "lmstudio", BAD, "the LM Studio server is down",
            detail=(
                "the desktop app being open does not start it. Every local request "
                "will fail over to a remote provider, which looks like success"
            ),
            fix=_fix_lmstudio_server, fix_label="start the server",
        )
    loaded = lmstudio.loaded_models()
    if not loaded:
        return Check("lmstudio", WARN, "server up, but no model is loaded",
                     detail="run `ruti model use <key>`")
    return Check(
        "lmstudio", OK,
        f"{len(loaded)} model(s) loaded",
        detail=", ".join(f"{m.identifier}@{m.loaded_context}" for m in loaded),
    )


def _check_generated_sync() -> Check:
    if not litellm_cfg.include_is_wired():
        return Check(
            "model-list", BAD, "config.yaml does not include models.generated.yaml",
            detail="no local model is reachable through the proxy",
            fix=_fix_sync, fix_label="regenerate and wire the include",
        )
    if not LITELLM_GENERATED.exists():
        return Check("model-list", BAD, "models.generated.yaml is missing",
                     detail="the proxy will refuse to start",
                     fix=_fix_sync, fix_label="regenerate it")

    on_disk = {planner.identifier_for(m.key) for m in lmstudio.list_models() if m.kind == "llm"}
    text = LITELLM_GENERATED.read_text(encoding="utf-8")
    declared = {line.split(":", 1)[1].strip() for line in text.splitlines()
                if line.strip().startswith("- model_name:")}
    missing, stale = on_disk - declared, declared - on_disk
    if missing or stale:
        parts = []
        if missing:
            parts.append("downloaded but not declared: " + ", ".join(sorted(missing)))
        if stale:
            parts.append("declared but no longer on disk: " + ", ".join(sorted(stale)))
        return Check("model-list", WARN, "the generated model list is out of date",
                     detail="; ".join(parts), fix=_fix_sync, fix_label="regenerate it")
    return Check("model-list", OK, f"{len(on_disk)} local model(s) declared and present")


def _check_route_reachable() -> Check:
    """Is the model that is actually loaded also the one the proxy can route to?

    The classic silent failure: the proxy advertises a local model that LM Studio no
    longer has resident, the request fails, the fallback answers from a remote
    provider, and nothing anywhere says the work left the machine.
    """
    if not litellm_cfg.liveliness() or not lmstudio.server_running():
        return Check("routing", WARN, "skipped -- proxy or LM Studio is down")

    served = set(litellm_cfg.served_models())
    resident = {m.identifier for m in lmstudio.loaded_models() if m.identifier}
    if not resident:
        return Check("routing", WARN, "no local model is resident to route to")

    unreachable = resident - served
    if unreachable:
        return Check(
            "routing", WARN,
            "loaded but not served: " + ", ".join(sorted(unreachable)),
            detail="run `ruti models sync` and restart the proxy",
            fix=_fix_sync, fix_label="regenerate the model list",
        )
    return Check("routing", OK, "resident models are routable: " + ", ".join(sorted(resident)))


def _check_opencode_drift() -> Check:
    if not LIVE_OPENCODE.exists():
        return Check("opencode", BAD, "no OpenCode config installed",
                     detail=f"expected {LIVE_OPENCODE}",
                     fix=_fix_opencode_drift, fix_label="install the repo copy")
    try:
        live = json.loads(LIVE_OPENCODE.read_text(encoding="utf-8"))
        repo = json.loads(REPO_OPENCODE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return Check("opencode", WARN, f"could not compare configs: {exc}")

    live_models = set((live.get("provider", {}).get("ruti-router", {}).get("models") or {}))
    repo_models = set((repo.get("provider", {}).get("ruti-router", {}).get("models") or {}))
    # Only models the repo has and the install lacks are drift (a version bump
    # added a baseline model this machine never picked up). Models the install
    # has beyond the repo's are locally registered providers (`provider add`,
    # `openrouter setup`) -- expected, not a problem, and never worth flagging.
    missing = repo_models - live_models
    if missing:
        return Check(
            "opencode", WARN, "the installed OpenCode config is missing repo models",
            detail=f"missing: {sorted(missing)}; installed has {len(live_models)} total",
            fix=_fix_opencode_drift, fix_label="merge the repo's models in",
        )
    extra = live_models - repo_models
    note = f" (plus {len(extra)} locally registered)" if extra else ""
    return Check("opencode", OK, f"config matches the repo ({len(live_models)} models){note}")


# Groups whose membership is effectively "anyone with a session on this box". A
# credentials file granted to any of these is readable by every process the machine
# runs, which for API keys means every installer, updater, and browser extension.
_BROAD_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-5-32-545": "Users",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-546": "Guests",
    "S-1-5-4": "Interactive",
}


def _acl_sids(path: Path) -> list[str] | None:
    """SIDs with an allow-ACE on `path`, or None if they cannot be read.

    icacls prints localised group names, so it cannot be matched against reliably on a
    non-English Windows. Translating to SIDs via .NET keeps the check language-neutral.
    """
    from . import proc

    script = (
        f"(Get-Acl -LiteralPath '{path}').Access | "
        "Where-Object { $_.AccessControlType -eq 'Allow' } | "
        "ForEach-Object { try { "
        "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value "
        "} catch { $_.IdentityReference.Value } }"
    )
    try:
        result = proc.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=25.0,
        )
    except (proc.ToolNotFound, proc.ToolTimeout):
        return None
    if not result.ok:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _check_env_permissions() -> Check:
    if not LITELLM_ENV.exists():
        return Check("secrets", BAD, "litellm/.env does not exist",
                     detail="copy .env.example and add your keys")

    sids = _acl_sids(LITELLM_ENV)
    if sids is None:
        return Check("secrets", OK, ".env present (permissions could not be read)")

    exposed = [name for sid, name in _BROAD_SIDS.items() if sid in sids]
    if exposed:
        return Check(
            "secrets", BAD,
            ".env grants access to " + ", ".join(exposed),
            detail=(
                "your API keys are readable by every process running as any local user. "
                "Restrict it with: icacls litellm\\.env /inheritance:d /remove:g "
                "\"*S-1-5-32-545\" \"*S-1-5-11\""
            ),
        )
    return Check("secrets", OK, f".env restricted to {len(sids)} privileged principal(s)")


def _git_ssl_backend() -> str | None:
    """The TLS backend git will actually use, or None if git is unavailable."""
    from . import proc

    try:
        result = proc.run(["git", "config", "--get", "http.sslBackend"], timeout=10.0)
    except (proc.ToolNotFound, proc.ToolTimeout):
        return None
    # Unset means git's compiled-in default. Git for Windows ships a system gitconfig
    # that sets openssl explicitly, so an empty answer here means a non-Windows build.
    return (result.stdout or "").strip().lower() or ""


def _fix_git_tls() -> str:
    from . import proc

    proc.run(
        ["git", "config", "--global", "http.sslBackend", "schannel"], timeout=15.0
    ).check()
    return "set git's global http.sslBackend to schannel (the Windows certificate store)"


def _check_git_tls() -> Check:
    """Does git trust the intercepting root, or does it fail the way everything else did?

    The `tls` check above covers Python, which verifies against certifi. git does not
    use certifi: Git for Windows ships its own `ca-bundle.crt` and defaults to the
    openssl backend, so it fails separately, with the same uninformative message, and
    the earlier check passing says nothing about it. Found while pushing this
    repository -- `ruti doctor` was entirely green at the time.

    The fix is to verify against the Windows certificate store, which does trust the
    root, rather than to stop verifying.
    """
    backend = _git_ssl_backend()
    if backend is None:
        return Check("git-tls", WARN, "git is not on PATH",
                     detail="cannot check whether it can reach an HTTPS remote")
    if backend == "schannel":
        return Check("git-tls", OK, "git verifies through the Windows certificate store")

    # Only a problem when something is actually intercepting; on a clean machine the
    # bundled roots are fine and there is nothing to fix.
    found = tls.detect()
    if not found.intercepted:
        return Check("git-tls", OK,
                     f"git uses {backend or 'its default backend'}; nothing is intercepting")

    return Check(
        "git-tls", BAD,
        f"git verifies against its own bundle while {found.issuer!r} intercepts TLS",
        detail=(
            "`git push` and `git clone` over HTTPS fail with `unable to get local "
            "issuer certificate`. The Windows certificate store does trust that root, "
            "so switching backends fixes it without weakening verification"
        ),
        fix=_fix_git_tls, fix_label="point git at the Windows certificate store",
    )


def _check_statusline() -> Check:
    """Is the status line registered, and is it actually producing readings?

    This check exists because its absence cost a day. The status line is the only local
    source of quota data, and when it does not run, nothing breaks -- `ruti route` just
    reports UNKNOWN forever and quietly routes as if the window were nearly spent. The
    two ways it silently dies are both checked here: a config Claude Code rewrote (it
    drops the unsupported `args` key, leaving a bare interpreter that prints nothing),
    and a config that looks right but has never produced a reading.
    """
    from . import install, quota

    try:
        settings = json.loads(install.SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Check("statusline", BAD, "settings.json is missing or unreadable",
                     detail="run `ruti install --apply`")

    configured = settings.get("statusLine") or {}
    command = str(configured.get("command") or "")

    if "ruti.statusline" not in command:
        return Check(
            "statusline", BAD,
            "ruti's status line is not registered" if not command
            else "statusLine points somewhere else",
            detail=(
                "without it there is no quota reading at all, and routing falls back to "
                "assuming the window is nearly spent"
                + (" -- note `args` is not a supported statusLine field and is dropped "
                   "by Claude Code" if configured.get("args") else "")
            ),
            fix=lambda: "registered ruti.statusline" if _fix_install() else "",
            fix_label="register it",
        )

    if "\\" in command:
        return Check(
            "statusline", BAD, "the status line command contains backslashes",
            detail=("Claude Code runs it through Git Bash, which consumes them as escapes; "
                    "the command then fails with no visible error. Use forward slashes."),
            fix=lambda: "rewrote the command with forward slashes" if _fix_install() else "",
            fix_label="rewrite it",
        )

    snapshot = quota.load()
    if snapshot.five_hour is None:
        return Check(
            "statusline", WARN, "registered, but no quota reading has ever arrived",
            detail=("restart Claude Code so it picks the setting up; if it is already "
                    "running, the reading appears on the next repaint"),
        )
    if snapshot.freshness in ("unknown", "never"):
        return Check(
            "statusline", WARN,
            f"last reading is stale ({snapshot.freshness})",
            detail="normal between sessions; routing assumes ORANGE until it refreshes",
        )
    return Check(
        "statusline", OK,
        f"live: {snapshot.five_hour.used_percentage:.0f}% of the 5h window used",
    )


def _fix_install() -> bool:
    from . import install

    changes = [c for c in install.plan() if c[0] == "settings.json"]
    if not changes:
        return False
    install.apply(changes)
    return True


CHECKS = (
    _check_statusline,
    _check_tls,
    _check_git_tls,
    _check_proxy_bind,
    _check_proxy_alive,
    _check_lmstudio,
    _check_generated_sync,
    _check_route_reachable,
    _check_opencode_drift,
    _check_env_permissions,
)


def run_checks() -> Report:
    report = Report()
    for check in CHECKS:
        try:
            report.checks.append(check())
        except Exception as exc:  # A broken check must not hide the healthy ones.
            report.checks.append(
                Check(check.__name__.removeprefix("_check_"), WARN,
                      f"check failed to run: {type(exc).__name__}: {exc}")
            )
    return report
