"""The `ruti` command line."""

from __future__ import annotations

import click

from . import delegate as delegate_mod
from . import doctor as doctor_mod
from . import install as install_mod
from . import providers as providers_mod
from . import ledger, lmstudio, litellm_cfg, planner, quota, router, tls, ui, vram
from .config import ensure_dirs, file_lock, load_dotenv


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="ruti")
def main() -> None:
    """Route implementation work to whichever model is cheapest and capable enough."""
    ensure_dirs()
    # Before any command touches the network: on a machine with intercepted TLS, this
    # is the difference between working and reporting every API key as invalid.
    tls.apply_to_environment()


# --------------------------------------------------------------------------- status


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def status(as_json: bool) -> None:
    """One screen: subscription budget, GPU, local models, proxy health."""
    snapshot = quota.load()
    gpu = vram.primary_gpu()
    server_up = lmstudio.server_running() if lmstudio.available() else False
    loaded = lmstudio.loaded_models() if server_up else []
    proxy_up = litellm_cfg.liveliness()

    payload = {
        "budget": {
            "band": snapshot.band,
            "five_hour_used_percentage": (
                None if snapshot.five_hour is None else snapshot.five_hour.used_percentage
            ),
            "seven_day_used_percentage": (
                None if snapshot.seven_day is None else snapshot.seven_day.used_percentage
            ),
            "freshness": snapshot.freshness,
            "summary": snapshot.summary(),
        },
        "gpu": None
        if gpu is None
        else {
            "name": gpu.name,
            "total_mib": gpu.total_mib,
            "free_mib": gpu.free_mib,
            "budget_mib": vram.budget_mib(gpu),
        },
        "lmstudio": {
            "server": "up" if server_up else "down",
            "loaded": [
                {"identifier": m.identifier, "key": m.key, "context": m.loaded_context,
                 "status": m.status, "queued": m.queued}
                for m in loaded
            ],
        },
        "proxy": {
            "liveliness": proxy_up,
            "models": litellm_cfg.served_models() if proxy_up else [],
            "include_wired": litellm_cfg.include_is_wired(),
        },
    }

    if as_json:
        ui.emit_json(payload)
        return

    # Budget first: it is the constraint every other line is subordinate to. Running out
    # of it stops all work, whereas a full GPU only costs an executor.
    ui.heading("Subscription budget")
    # `summary()` already opens with the band, so it is not repeated here.
    ui.say(f"  [head]{ui.literal(snapshot.summary())}[/head]")
    if snapshot.seven_day is not None:
        ui.say(f"  [muted]{snapshot.seven_day.used_percentage:.0f}% of the 7d window used[/muted]")
    ui.say(f"  [muted]{ui.literal(quota.BAND_POLICY[snapshot.band]['guidance'])}[/muted]")

    ui.heading("GPU")
    if gpu is None:
        ui.warn("no NVIDIA GPU detected -- local model sizing is unavailable")
    else:
        ui.say(
            f"  {gpu.name}: {ui.human_mib(gpu.free_mib)} free of {ui.human_mib(gpu.total_mib)}"
            f"  (usable budget {ui.human_mib(vram.budget_mib(gpu))})"
        )

    ui.heading("LM Studio")
    if not server_up:
        ui.bad("server is DOWN -- every local request will fail over to a remote provider")
        ui.say("  [muted]start it with `lms server start`, or run `ruti doctor --fix`[/muted]")
    elif not loaded:
        ui.warn("server up, but no model is loaded")
    else:
        for model in loaded:
            ui.ok(f"{model.identifier}  ctx {model.loaded_context}  "
                  f"({model.status}, {model.queued} queued)")
            # Whether and when the card frees itself. Without this the only way to find
            # out a model is squatting on the GPU is to try to start something else and
            # have it fail.
            if model.ttl_ms:
                ui.say(f"  [muted]holds ~{planner.footprint(model)} MiB; releases it "
                       f"after {model.ttl_ms // 60000} min idle[/muted]")
            else:
                ui.warn(f"  no idle timeout -- holds the GPU until "
                        f"`ruti model unload {model.identifier}`")

    ui.heading("LiteLLM proxy")
    if proxy_up:
        ui.ok("alive: " + ", ".join(payload["proxy"]["models"]))
    else:
        ui.bad("not responding on /health/liveliness")


# --------------------------------------------------------------------------- models


@main.group(invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def models(ctx: click.Context, as_json: bool) -> None:
    """List local models and whether they fit."""
    if ctx.invoked_subcommand is not None:
        return

    gpu = vram.primary_gpu()
    budget = vram.budget_mib(gpu) if gpu else 0
    loaded_keys = {m.key: m for m in lmstudio.loaded_models()} if lmstudio.server_running() else {}

    # Report what would fit on an *idle* card rather than what fits right now: a model
    # that is loadable after a swap must not read as permanently impossible, and the
    # currently loaded model must not appear not to fit itself.
    idle_budget = budget + sum(planner.footprint(m) for m in loaded_keys.values())

    rows = []
    for model in lmstudio.list_models():
        if model.kind != "llm":
            continue
        fit = vram.largest_fitting_context(model, idle_budget)
        rows.append(
            {
                "key": model.key,
                "size_mib": model.size_mib,
                "tool_use": model.tool_use,
                "max_context": model.max_context,
                "fits_context": fit[0] if fit else None,
                "loaded": model.key in loaded_keys,
                "loaded_context": loaded_keys[model.key].loaded_context if model.key in loaded_keys else None,
                "identifier": planner.identifier_for(model.key),
            }
        )

    if as_json:
        ui.emit_json({"budget_mib": budget, "models": rows})
        return

    grid = ui.table("MODEL", "SIZE", "TOOLS", "MAX CTX", "FITS AT", "STATE")
    for row in rows:
        fits = str(row["fits_context"]) if row["fits_context"] else "[bad]does not fit[/bad]"
        state = (
            f"[ok]loaded @ {row['loaded_context']}[/ok]" if row["loaded"] else "[muted]on disk[/muted]"
        )
        grid.add_row(
            row["key"],
            ui.human_mib(row["size_mib"]),
            "[ok]yes[/ok]" if row["tool_use"] else "[bad]NO[/bad]",
            str(row["max_context"]),
            fits,
            state,
        )
    ui.console.print(grid)

    usable = [r for r in rows if r["tool_use"] and r["fits_context"]]
    if not usable:
        ui.warn("no model on disk both fits this GPU and does structured tool calls -- "
                "delegation to a local model is not possible until you download one that does")

    # Say plainly whether co-residency is even reachable, instead of implying the
    # planner deliberates over it when the hardware forecloses it.
    fitting = sorted((r["size_mib"] for r in rows if r["fits_context"]))
    if len(fitting) >= 2 and sum(fitting[:2]) > budget:
        ui.say("[muted]note: no two of these fit at once; `ruti model use` will always swap[/muted]")


@models.command("sync")
@click.option("--json", "as_json", is_flag=True)
def models_sync(as_json: bool) -> None:
    """Regenerate the LiteLLM model list from what is on disk."""
    entries, names = [], []
    for model in lmstudio.list_models():
        if model.kind != "llm":
            continue
        identifier = planner.identifier_for(model.key)
        entries.append(litellm_cfg.local_entry(identifier))
        names.append(identifier)

    litellm_cfg.write_generated(entries)
    wired = litellm_cfg.wire_include()
    # OpenCode only offers models its own config declares, so a model that is served by
    # the proxy but missing here fails under `opencode` with an opaque server error.
    written = litellm_cfg.sync_opencode(litellm_cfg.routable_aliases())

    if as_json:
        ui.emit_json({"models": names, "include_added": wired,
                      "opencode_updated": [str(p) for p in written]})
        return
    ui.ok(f"wrote {len(names)} local entries: {', '.join(names)}")
    ui.ok(f"updated {len(written)} OpenCode config(s) with every routable alias")
    if wired:
        ui.ok("added the `include:` line to config.yaml -- restart the proxy once to pick it up")
    else:
        ui.say("[muted]config.yaml already includes the generated file[/muted]")


# ---------------------------------------------------------------------------- model


@main.group()
def model() -> None:
    """Load and unload local models."""


@model.command("use")
@click.argument("key")
@click.option("--context", "-c", type=int, default=None, help="Preferred context window.")
@click.option("--min-context", type=int, default=vram.CONTEXT_FLOOR,
              help="Refuse rather than load below this window.")
@click.option("--ttl", type=int, default=lmstudio.DEFAULT_TTL_SECONDS, show_default=True,
              help="Auto-unload after N idle seconds; 0 to stay resident indefinitely.")
@click.option("--dry-run", is_flag=True, help="Show the plan without executing it.")
@click.option("--json", "as_json", is_flag=True)
def model_use(key: str, context: int | None, min_context: int, ttl: int | None,
              dry_run: bool, as_json: bool) -> None:
    """Make KEY resident, loading alongside or swapping as memory allows."""
    catalog = {m.key: m for m in lmstudio.list_models()}
    if key not in catalog:
        raise click.ClickException(
            f"{key!r} is not downloaded. Available: {', '.join(sorted(catalog))}"
        )

    if not lmstudio.server_running():
        ui.warn("LM Studio server was down; starting it")
        lmstudio.start_server()

    # Held across plan and execute: two sessions swapping at once would interleave
    # loads and unloads and thrash the card.
    with file_lock("model-swap", timeout=180.0):
        plan = planner.plan_load(catalog[key], preferred_context=context, min_context=min_context)

        if as_json and dry_run:
            ui.emit_json({"action": plan.action, "context": plan.context,
                          "evict": [v.identifier for v in plan.evict],
                          "reasons": plan.reasons, "deficit_mib": plan.deficit_mib})
            return

        for reason in plan.reasons:
            (ui.warn if plan.action == "refuse" else ui.say)(f"  {reason}")

        if not plan.ok:
            raise click.ClickException(f"cannot load {key!r}")
        if dry_run:
            ui.ok(f"plan: {plan.action} at context {plan.context}")
            return

        report = planner.execute(plan, ttl_seconds=ttl or None)

    if report.get("spilled"):
        ui.warn("measured VRAM is well under the prediction while the card is full -- "
                "part of the model is probably in shared system memory and will be slow")

    if as_json:
        ui.emit_json(report)
        return

    if report["action"] == "noop":
        ui.ok(f"{report['identifier']} already loaded at {report['context']}")
    else:
        ui.ok(
            f"{report['identifier']} loaded at {report['context']} in {report['load_seconds']}s"
            f"  (predicted {ui.human_mib(report['predicted_mib'])},"
            f" measured {ui.human_mib(report['measured_mib'])})"
        )
        if report["evicted"]:
            ui.say(f"  [muted]unloaded: {', '.join(report['evicted'])}[/muted]")


@main.command()
@click.option("--days", type=float, default=None, help="Only count the last N days.")
@click.option("--json", "as_json", is_flag=True)
def report(days: float | None, as_json: bool) -> None:
    """Show what delegation has actually bought you."""
    events = ledger.read_events(since_days=days)
    stats = ledger.summarise(events)

    if as_json:
        ui.emit_json(stats)
        return
    if not events:
        ui.say("[muted]nothing recorded yet -- the ledger fills as you delegate[/muted]")
        return

    d = stats["delegations"]
    w, t, s = stats["work_offloaded"], stats["transcript_contained"], stats["sessions"]

    ui.heading("Delegations")
    if not d["total"]:
        ui.say("  [muted]none yet[/muted]")
    else:
        ui.say(f"  {d['total']} run, {d['failed']} failed")
        if d["substituted"]:
            ui.warn(f"  {d['substituted']} were silently answered by a fallback provider "
                    "-- work you routed locally did not stay local")
        grid = ui.table("TIER", "RUNS", "FAILED", "TOTAL TIME", "LINES WRITTEN")
        for tier, entry in sorted(d["by_tier"].items()):
            grid.add_row(tier, str(entry["runs"]), str(entry["failed"]),
                         f"{entry['seconds']:.0f}s", f"{entry['lines']:,}")
        ui.console.print(grid)

    ui.heading("Work the manager did not have to type")
    ui.say(f"  [ok]{w['lines_written']:,} lines written by delegates[/ok]  "
           f"[muted](~{w['approx_tokens']:,} tokens at {ledger.TOKENS_PER_LINE}/line)[/muted]")
    ui.say("  [muted]had the manager written these, every line would have passed through "
           "its context as tool input[/muted]")
    if w.get("discarded_lines"):
        ui.say(f"  [warn]{w['discarded_lines']:,} further lines came from runs that failed "
               f"and are not counted[/warn]")
    ui.say("  [muted]an upper bound: a run counts as successful when the process exited "
           "cleanly and any Python it wrote parses, which is weaker than the work being "
           "usable[/muted]")

    ui.heading("Delegate transcript kept in logs")
    ui.say(f"  emitted {t['delegate_output_bytes'] / 1024:.0f} KB, "
           f"summarised to {t['summary_bytes'] / 1024:.0f} KB, "
           f"[ok]{t['bytes'] / 1024:.0f} KB never read[/ok]")
    ui.say("  [muted]measurement corrected an assumption here: opencode's own output is "
           "terse, so containing it saves far less than the generated code does[/muted]")

    if stats["routes"]["total"]:
        ui.heading("Routing")
        ui.say(f"  {stats['routes']['total']} rankings requested")

    ui.heading("Window utilisation per session")
    if s["with_usable_quota_readings"] < 2:
        ui.say(f"  [muted]{s['seen']} session(s) seen, {s['with_usable_quota_readings']} with "
               "usable readings at both ends. Needs a few more before the comparison "
               "means anything.[/muted]")
    else:
        with_d = s["mean_five_hour_spend_with_delegation"]
        without = s["mean_five_hour_spend_without"]
        ui.say(f"  delegating    : {with_d:.1f} points per session  ({s['counted_with']} sessions)"
               if with_d is not None else "  delegating    : no data yet")
        ui.say(f"  not delegating: {without:.1f} points per session  ({s['counted_without']} sessions)"
               if without is not None else "  not delegating: no data yet")
    if s["spanning_a_reset_excluded"]:
        ui.say(f"  [muted]{s['spanning_a_reset_excluded']} session(s) excluded for spanning a "
               "window reset[/muted]")

    ui.say("")
    ui.say("[muted]The two figures above are direct measurements. The utilisation numbers are "
           "not: the same window is shared with every other project, and no counterfactual "
           "was ever run, so they are observations rather than attribution.[/muted]")


@main.command()
@click.option("--apply", "do_apply", is_flag=True, help="Write the changes, not just show them.")
def install(do_apply: bool) -> None:
    """Wire ruti into Claude Code: status line, hooks, agents, and the policy file."""
    try:
        changes = install_mod.plan()
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    if not changes:
        ui.ok("everything is already installed and current")
        return

    for label, path, before, after in changes:
        ui.heading(f"{label}  [muted]{ui.literal(str(path))}[/muted]")
        rendered = install_mod.diff(label, before, after)
        if not rendered:
            ui.say("  [muted](new file)[/muted]")
            continue
        for line in rendered.splitlines()[:40]:
            style = "ok" if line.startswith("+") else "bad" if line.startswith("-") else "muted"
            ui.say(f"  [{style}]{ui.literal(line)}[/{style}]")

    if not do_apply:
        ui.say("")
        ui.say("[muted]this was a preview -- rerun with `--apply` to write it[/muted]")
        return

    for note in install_mod.apply(changes):
        ui.ok(note)
    ui.say("")
    ui.say("[muted]the status line and hooks take effect in the next Claude Code session; "
           "run `/hooks` there to confirm they parsed[/muted]")


# ---------------------------------------------------------------------------- route


@main.command()
@click.option("--kind", default="implement",
              type=click.Choice(["boilerplate", "implement", "refactor", "debug",
                                 "analyze", "review", "security"]),
              help="What sort of work this is.")
@click.option("--files", type=int, default=1, help="How many files it touches.")
@click.option("--loc", type=int, default=50, help="Rough lines of code involved.")
@click.option("--needs-tools/--no-tools", default=True,
              help="Whether the executor must write files itself.")
@click.option("--repo-context", type=click.Choice(["none", "small", "large"]), default="small",
              help="How much of the repository has to be in context.")
@click.option("--risk", type=click.Choice(["low", "medium", "high"]), default="low")
@click.option("--latency", type=click.Choice(["interactive", "background"]), default="background")
@click.option("--json", "as_json", is_flag=True)
def route(kind: str, files: int, loc: int, needs_tools: bool, repo_context: str,
          risk: str, latency: str, as_json: bool) -> None:
    """Rank the executors for a task you have already classified."""
    task = router.Task(kind=kind, files=files, loc=loc, needs_tools=needs_tools,
                       repo_context=repo_context, risk=risk, latency=latency)
    result = router.rank(task)

    if as_json:
        ui.emit_json(result)
        return

    q = result["quota"]
    freshness = "" if q["freshness"] == "live" else f" [warn]({q['freshness']})[/warn]"
    used = f"{q['five_hour_used']:.0f}%" if q["five_hour_used"] is not None else "?"
    ui.say(f"[head]{q['band']}[/head]  {used} of the 5h window used{freshness}")
    ui.say(f"[muted]task: {result['task']['kind']}, ~{result['task']['estimated_tokens']} tokens, "
           f"difficulty {result['task']['difficulty']}[/muted]")

    ui.heading("Eligible")
    for entry in result["ranked"]:
        ui.say(f"  [ok]{entry['score']:.2f}[/ok]  {entry['executor']}  "
               f"[muted]{entry['command']}[/muted]")
        for reason in entry["reasons"]:
            ui.say(f"        [muted]- {ui.literal(reason)}[/muted]")

    if result["rejected"]:
        ui.heading("Ruled out")
        for entry in result["rejected"]:
            ui.say(f"  [bad]x[/bad]  {entry['executor']}: {ui.literal(entry['blockers'][0])}")

    ui.heading("Advice")
    ui.say(f"  {ui.literal(result['advice'])}")


@main.command()
@click.option("--model", required=True, help="Proxy model name, e.g. local-qwen3-4b.")
@click.option("--task", default=None, help="The instruction. Use --task-file for anything long.")
@click.option("--task-file", type=click.Path(exists=True), default=None)
@click.option("--dir", "directory", type=click.Path(exists=True), default=".",
              help="Where the work belongs.")
@click.option("--timeout", type=float, default=delegate_mod.DEFAULT_TIMEOUT)
@click.option("--json", "as_json", is_flag=True)
def delegate(model: str, task: str | None, task_file: str | None, directory: str,
             timeout: float, as_json: bool) -> None:
    """Run a task through `opencode` and report back in a few lines."""
    from pathlib import Path

    if task_file:
        text = Path(task_file).read_text(encoding="utf-8")
    elif task:
        text = task
    else:
        raise click.ClickException("give either --task or --task-file")

    qualified = model if "/" in model else f"ruti-router/{model}"
    outcome = delegate_mod.run(
        text, model=qualified, directory=Path(directory).resolve(), timeout=timeout
    )

    if as_json:
        ui.emit_json(outcome.summary())
        raise SystemExit(0 if outcome.ok else 1)

    if outcome.substituted:
        ui.warn(
            f"you asked for {outcome.model_requested} but the proxy answered as "
            f"{outcome.model_answering} -- a backend is down and the fallback took over. "
            "Work you meant to keep on this machine did not stay here."
        )
    if outcome.ok:
        ui.ok(f"{outcome.model_requested} finished in {outcome.duration_s:.0f}s")
    else:
        ui.bad(f"{outcome.model_requested} failed "
               f"({outcome.error or f'exit {outcome.exit_code}'}) after {outcome.duration_s:.0f}s")
    if outcome.files_changed:
        ui.say("  changed: " + ", ".join(outcome.files_changed))
    if outcome.broken_files:
        ui.warn("  the delegate exited cleanly but left Python that does not parse:")
        for entry in outcome.broken_files:
            ui.say(f"    [bad]{ui.literal(entry)}[/bad]")
    if outcome.diff_stat:
        for line in outcome.diff_stat.splitlines():
            ui.say(f"  [muted]{ui.literal(line)}[/muted]")
    if outcome.tail:
        ui.say("[muted]  --- last lines ---[/muted]")
        for line in outcome.tail.splitlines():
            ui.say(f"  [muted]{ui.literal(line)}[/muted]")
    ui.say(f"[muted]  full log: {ui.literal(outcome.log_path)}[/muted]")
    raise SystemExit(0 if outcome.ok else 1)


# ------------------------------------------------------------------------- provider


@main.group()
def provider() -> None:
    """Add and check remote API providers."""


def _pick_provider() -> str:
    ui.say("\n[head]Which provider?[/head]")
    for number, (name, blurb) in enumerate(providers_mod.FEATURED, start=1):
        ui.say(f"  [ok]{number:>2}[/ok]  {name:<14} [muted]{blurb}[/muted]")
    ui.say(f"  [ok]{len(providers_mod.FEATURED) + 1:>2}[/ok]  other        "
           "[muted]anything else litellm supports, or a custom endpoint[/muted]")

    choice = click.prompt("Number", type=int)
    if 1 <= choice <= len(providers_mod.FEATURED):
        return providers_mod.FEATURED[choice - 1][0]

    known = providers_mod.known_providers()
    ui.say(f"[muted]{len(known)} providers are available; type part of a name to filter[/muted]")
    while True:
        needle = click.prompt("Provider name").strip().lower()
        matches = [name for name in known if needle in name]
        if not matches:
            ui.warn("no match")
            continue
        if len(matches) == 1:
            return matches[0]
        ui.say("  " + ", ".join(matches[:25]))


def _report(verdict: providers_mod.Verdict) -> None:
    for stage in verdict.stages:
        mark = {"pass": ui.ok, "fail": ui.bad, "skip": ui.say}[stage.result]
        timing = f" [muted]({stage.latency_ms} ms)[/muted]" if stage.latency_ms else ""
        prefix = "    " if stage.result == "skip" else ""
        mark(f"{prefix}{stage.name:<10} {ui.literal(stage.detail)}{timing}")


@provider.command("add")
@click.option("--provider", "provider_name", default=None, help="Skip the picker.")
@click.option("--model", default=None, help="Model id to register.")
@click.option("--alias", default=None, help="Name to route to through the proxy.")
@click.option("--api-base", default=None, help="Override the endpoint (custom deployments).")
@click.option("--key-stdin", is_flag=True, help="Read the key from stdin instead of prompting.")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation before writing.")
def provider_add(provider_name: str | None, model: str | None, alias: str | None,
                 api_base: str | None, key_stdin: bool, yes: bool) -> None:
    """Add a provider. Nothing is written unless the key proves itself first."""
    import sys

    name = provider_name or _pick_provider()
    expected = providers_mod.env_var_for(name)

    # An existing environment variable is worth offering rather than making the user
    # dig the key out again -- but it is also worth saying out loud that a plaintext
    # user variable is readable by every process on the machine.
    import os

    key = ""
    if key_stdin:
        key = sys.stdin.read().strip()
    elif os.environ.get(expected):
        ui.say(f"\n[warn]{expected}[/warn] is already set in your environment "
               f"({ui.mask(os.environ[expected])})")
        ui.say("[muted]note: environment variables are readable by every process you run[/muted]")
        if click.confirm("Use it?", default=True):
            key = os.environ[expected]

    while not key:
        key = click.prompt("API key", hide_input=True).strip()
        if len(key) < 12:
            ui.warn("that looks too short to be a key -- did the paste get truncated?")
            key = ""

    if not model:
        model = click.prompt("Model id (e.g. gemini-2.5-flash)").strip()
    qualified = model if "/" in model else f"{name}/{model}"
    alias = alias or click.prompt("Route it through the proxy as", default=model.split("/")[-1])

    ui.heading(f"Testing {qualified}")
    verdict = providers_mod.test_key(name, qualified, key, api_base=api_base)
    _report(verdict)

    if not verdict.usable:
        failure = verdict.failure
        raise click.ClickException(
            f"not saved -- {failure.detail if failure else 'the key did not pass'}"
        )
    if not verdict.supports_tools:
        ui.warn("this model cannot drive `opencode`; it will be registered for text only")

    index = providers_mod.next_key_index(name)
    env_var = providers_mod.ruti_env_var(name, index)

    ui.heading("About to write")
    ui.say(f"  litellm/.env                    {env_var}=<your key>")
    ui.say(f"  litellm/providers.generated.yaml  {alias} -> {qualified}")
    ui.say(f"  ruti providers.json             registry entry {name}#{index}")
    if not yes and not click.confirm("Write these?", default=True):
        ui.say("[muted]nothing written[/muted]")
        return

    doctor_mod._set_env_var(env_var, key)
    registry = providers_mod.load_registry()
    registry["providers"].append(
        {
            "provider": name,
            "alias": alias,
            "model": qualified,
            "env_var": env_var,
            "api_base": api_base,
            "supports_tools": verdict.supports_tools,
            "enabled": True,
            "verified_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        }
    )
    providers_mod.save_registry(registry)
    litellm_cfg.write_providers(providers_mod.litellm_entries(registry))
    litellm_cfg.wire_include()

    # Declare it to OpenCode as well. A model the proxy serves but OpenCode has never
    # heard of fails with an opaque "Unexpected server error" that says nothing about
    # the actual cause.
    aliases = sorted(set(litellm_cfg.routable_aliases()) | {alias})
    litellm_cfg.sync_opencode(aliases)

    ui.ok(f"{alias} registered in LiteLLM and OpenCode -- restart the proxy to route to it "
          "(`Stop-ScheduledTask`/`Start-ScheduledTask -TaskName RutiLiteLLM`)")


@provider.command("list")
@click.option("--json", "as_json", is_flag=True)
def provider_list(as_json: bool) -> None:
    """Show registered providers."""
    registry = providers_mod.load_registry()
    env = load_dotenv()

    if as_json:
        ui.emit_json(registry)
        return
    if not registry["providers"]:
        ui.say("[muted]no providers registered; add one with `ruti provider add`[/muted]")
        return

    grid = ui.table("ALIAS", "MODEL", "TOOLS", "KEY", "VERIFIED")
    for record in registry["providers"]:
        secret = env.get(record["env_var"], "")
        grid.add_row(
            record["alias"], record["model"],
            "[ok]yes[/ok]" if record.get("supports_tools") else "[bad]no[/bad]",
            f"{record['env_var']} = {ui.mask(secret)}" if secret else "[bad]MISSING[/bad]",
            record.get("verified_at", "?"),
        )
    ui.console.print(grid)


@provider.command("test")
@click.argument("alias", required=False)
@click.option("--json", "as_json", is_flag=True)
def provider_test(alias: str | None, as_json: bool) -> None:
    """Re-run the key checks for one or all registered providers."""
    registry = providers_mod.load_registry()
    env = load_dotenv()
    targets = [r for r in registry["providers"] if alias is None or r["alias"] == alias]
    if not targets:
        raise click.ClickException(f"no provider registered as {alias!r}")

    results = []
    for record in targets:
        key = env.get(record["env_var"], "")
        if not key:
            ui.bad(f"{record['alias']}: {record['env_var']} is missing from .env")
            results.append({"alias": record["alias"], "usable": False, "reason": "key missing"})
            continue
        ui.heading(f"{record['alias']} -> {record['model']}")
        verdict = providers_mod.test_key(
            record["provider"], record["model"], key, api_base=record.get("api_base")
        )
        if not as_json:
            _report(verdict)
        results.append({
            "alias": record["alias"], "usable": verdict.usable,
            "supports_tools": verdict.supports_tools,
            "stages": [{"name": s.name, "result": s.result, "detail": s.detail}
                       for s in verdict.stages],
        })

    if as_json:
        ui.emit_json(results)
    raise SystemExit(0 if all(r["usable"] for r in results) else 1)


@provider.command("remove")
@click.argument("alias")
@click.option("--yes", is_flag=True)
def provider_remove(alias: str, yes: bool) -> None:
    """Remove a provider. The key stays in .env unless you say otherwise."""
    registry = providers_mod.load_registry()
    keep = [r for r in registry["providers"] if r["alias"] != alias]
    removed = [r for r in registry["providers"] if r["alias"] == alias]
    if not removed:
        raise click.ClickException(f"no provider registered as {alias!r}")

    if not yes and not click.confirm(f"Remove {len(removed)} entry/entries for {alias!r}?",
                                     default=False):
        return
    registry["providers"] = keep
    providers_mod.save_registry(registry)
    litellm_cfg.write_providers(providers_mod.litellm_entries(registry))
    ui.ok(f"removed {alias}")
    ui.say("[muted]its key is still in litellm/.env -- delete the "
           f"{', '.join(r['env_var'] for r in removed)} line(s) if you want it gone[/muted]")


@main.command()
@click.option("--fix", is_flag=True, help="Apply the automatic fixes for what is broken.")
@click.option("--json", "as_json", is_flag=True)
def doctor(fix: bool, as_json: bool) -> None:
    """Check for the failures that would otherwise stay silent."""
    report = doctor_mod.run_checks()

    if fix:
        for check in report.fixable:
            ui.say(f"[warn]fixing[/warn] {check.name}: {check.fix_label}")
            try:
                ui.ok("  " + check.fix())
            except Exception as exc:
                ui.bad(f"  fix failed: {type(exc).__name__}: {exc}")
        report = doctor_mod.run_checks()

    if as_json:
        ui.emit_json(
            {
                "status": report.worst,
                "checks": [
                    {"name": c.name, "status": c.status, "message": c.message,
                     "detail": c.detail, "fixable": bool(c.fix and c.status != "ok")}
                    for c in report.checks
                ],
            }
        )
        raise SystemExit(0 if report.worst != "bad" else 1)

    icon = {"ok": ui.ok, "warn": ui.warn, "bad": ui.bad}
    for check in report.checks:
        icon[check.status](f"{check.name:<12} {ui.literal(check.message)}")
        if check.detail:
            ui.say(f"      [muted]{ui.literal(check.detail)}[/muted]")

    remaining = report.fixable
    if remaining and not fix:
        ui.say("")
        ui.say(f"[muted]{len(remaining)} of these can be fixed automatically: run `ruti doctor --fix`[/muted]")
    raise SystemExit(0 if report.worst != "bad" else 1)


@model.command("unload")
@click.argument("identifier", required=False)
@click.option("--all", "unload_all", is_flag=True, help="Unload every loaded model.")
def model_unload(identifier: str | None, unload_all: bool) -> None:
    """Unload a model by IDENTIFIER."""
    loaded = lmstudio.loaded_models()
    if not loaded:
        ui.say("[muted]nothing is loaded[/muted]")
        return

    targets = [m.identifier for m in loaded] if unload_all else [identifier]
    if not unload_all and not identifier:
        raise click.ClickException(
            "name an identifier or pass --all. Loaded: "
            + ", ".join(m.identifier or "?" for m in loaded)
        )

    with file_lock("model-swap", timeout=180.0):
        for target in targets:
            if target:
                lmstudio.unload(target)
                ui.ok(f"unloaded {target}")


if __name__ == "__main__":
    main()
