"""The `ruti` command line."""

from __future__ import annotations

import click

from . import council as council_mod
from . import delegate as delegate_mod
from . import doctor as doctor_mod
from . import install as install_mod
from . import providers as providers_mod
from . import usage as usage_mod
from . import modes as modes_mod
from . import openrouter as openrouter_mod
from . import ledger, lmstudio, litellm_cfg, planner, quota, router, sessions, tls, ui, vram
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
    session_id = sessions.current_session_id()
    session_disabled = sessions.is_disabled(session_id)

    payload = {
        "session": {"id": session_id, "disabled": session_disabled},
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

    if session_disabled:
        ui.warn("ruti is OFF for this session -- `route`/`delegate` refuse; `ruti on` to resume")

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

    _report_money(stats["money"])

    ui.heading("Work the manager did not have to type")
    ui.say(f"  [ok]{w['lines_written']:,} lines written by delegates[/ok]  "
           f"[muted](~{w['approx_tokens']:,} tokens at {ledger.TOKENS_PER_LINE}/line)[/muted]")
    ui.say("  [muted]had the manager written these, every line would have passed through "
           "its context as tool input[/muted]")
    if w.get("discarded_lines"):
        ui.say(f"  [warn]{w['discarded_lines']:,} further lines came from runs that failed "
               f"and are not counted[/warn]")
    if w.get("files"):
        shown = ", ".join(w["files"][:8])
        more = f" (+{len(w['files']) - 8} more)" if len(w["files"]) > 8 else ""
        ui.say(f"  [muted]written by delegates: {shown}{more}[/muted]")
    ui.say("  [muted]an upper bound: a run counts as successful when the process exited "
           "cleanly and any Python it wrote parses, which is weaker than the work being "
           "usable[/muted]")

    ui.heading("Delegate transcript kept in logs")
    ui.say(f"  emitted {t['delegate_output_bytes'] / 1024:.0f} KB, "
           f"summarised to {t['summary_bytes'] / 1024:.0f} KB, "
           f"[ok]{t['bytes'] / 1024:.0f} KB never read[/ok]")
    ui.say("  [muted]measurement corrected an assumption here: opencode's own output is "
           "terse, so containing it saves far less than the generated code does[/muted]")

    r = stats["routes"]
    if r["total"]:
        ui.heading("Routing")
        ui.say(f"  {r['total']} ranking(s) requested: [ok]{r['followed']} followed[/ok], "
               f"{r['recommended_self']} recommended this session, "
               + (f"[warn]{r['ignored']} ignored[/warn]" if r["ignored"] else "0 ignored"))
        if r["ignored"]:
            ui.say("  [muted]ignored means the ranking named a delegate and no delegation "
                   "to it followed in that session[/muted]")

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


def _report_money(money: dict) -> None:
    ui.heading("Money")
    if not money["by_provider"]:
        ui.say("  [muted]no delegations[/muted]")
        return
    if not money["counted"]:
        ui.warn("  money is not counted: no remote delegation here carries a price its "
                "provider stated")
        ui.say("  [muted]ruti records cost only from the provider's own response (OpenRouter "
               "states it per request); runs from before this was tracked, and providers "
               "that state no price, are counted as runs but not as money[/muted]")
    grid = ui.table("PROVIDER", "RUNS", "COSTED", "USD")
    for provider, row in sorted(money["by_provider"].items()):
        costed = f"{row['costed_runs']}/{row['runs']}"
        usd = f"${row['usd']:.4f}" if row["costed_runs"] else "[muted]not counted[/muted]"
        grid.add_row(provider, str(row["runs"]),
                     costed if row["costed_runs"] == row["runs"] else f"[warn]{costed}[/warn]",
                     usd)
    ui.console.print(grid)
    if money["counted"] and money["uncosted_runs"]:
        ui.say(f"  [warn]{money['uncosted_runs']} run(s) carry no stated price -- the total "
               f"is a lower bound[/warn]")
    if money["by_model"]:
        grid = ui.table("MODEL THAT ACTUALLY ANSWERED", "RUNS", "REQUESTS", "USD")
        ranked = sorted(money["by_model"].items(),
                        key=lambda item: (item[1]["usd"] or 0.0, item[1]["requests"]),
                        reverse=True)
        for model, row in ranked:
            grid.add_row(ui.literal(model), str(row["runs"]), str(row["requests"]),
                         f"${row['usd']:.4f}" if row["usd"] is not None
                         else "[muted]not stated[/muted]")
        ui.console.print(grid)


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


# ------------------------------------------------------------------------- on / off


def _session_or_die() -> str:
    session_id = sessions.current_session_id()
    if not session_id:
        raise click.ClickException(
            f"no ${sessions.ENV_VAR} in the environment -- this only works run from "
            "inside a Claude Code session"
        )
    return session_id


@main.command()
def off() -> None:
    """Disable `route`/`delegate` for the current Claude Code session only."""
    session_id = _session_or_die()
    sessions.set_disabled(session_id, True)
    ui.ok(f"ruti disabled for this session ({session_id[:8]}) -- "
          "`route` and `delegate` will refuse until `ruti on`")


@main.command()
def on() -> None:
    """Re-enable `route`/`delegate` for the current Claude Code session."""
    session_id = _session_or_die()
    was_disabled = sessions.is_disabled(session_id)
    sessions.set_disabled(session_id, False)
    if was_disabled:
        ui.ok("ruti re-enabled for this session")
    else:
        ui.say("[muted]ruti was already enabled for this session[/muted]")


# ----------------------------------------------------------------------- mode


@main.group("mode", invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
@click.pass_context
def mode(ctx: click.Context, as_json: bool) -> None:
    """Session task modes: `coding` biases delegation toward the coding harness and
    coding-tuned models; `free` keeps it on zero-cost models. Both are scoped to this
    Claude Code session, like `ruti off`."""
    if ctx.invoked_subcommand is not None:
        return

    session_id = sessions.current_session_id()
    state = modes_mod.current(session_id)

    if as_json:
        ui.emit_json({"session": session_id, **state})
        return

    if not session_id:
        ui.warn(f"no ${sessions.ENV_VAR} in the environment -- modes only mean something "
                "run from inside a Claude Code session")
    ui.say(f"  coding : {'[ok]on[/ok]' if state['coding'] else '[muted]off[/muted]'}")
    free = state["free"]
    ui.say(f"  free   : {'[muted]off[/muted]' if free == 'off' else f'[ok]{free}[/ok]'}")
    summary = modes_mod.active_summary(state)
    if summary:
        ui.say(f"\n[muted]active: {summary}[/muted]")


@mode.command("coding")
@click.argument("state", type=click.Choice(["on", "off"]))
def mode_coding(state: str) -> None:
    """Toggle the coding hint. On: the prompt hook tells the manager to prefer the
    `pareto-code` router and coding-tuned models whenever it delegates."""
    session_id = _session_or_die()
    modes_mod.set_coding(session_id, state == "on")
    if state == "on":
        ui.ok("coding mode on -- the manager will be told to reach for `pareto-code` and "
              "coding models when it delegates (register them with `ruti openrouter setup`)")
    else:
        ui.ok("coding mode off")


@mode.command("free")
@click.argument("level", type=click.Choice(["off", "soft", "hard"]))
def mode_free(level: str) -> None:
    """Prefer zero-cost models. `soft` flags and deprioritises paid APIs; `hard`
    makes `route` rule them out and `delegate` refuse them."""
    session_id = _session_or_die()
    modes_mod.set_free(session_id, level)
    ui.ok({
        "off": "free mode off",
        "soft": "free mode: soft -- paid metered APIs are flagged and deprioritised, not blocked",
        "hard": "free mode: hard -- `route` rules out paid metered APIs, `delegate` refuses them",
    }[level])


def _free_status_of(alias: str) -> bool | None:
    """True if the alias is a confirmed zero-cost model, False if confirmed paid,
    None if ruti has no record of it either way."""
    for record in providers_mod.load_registry()["providers"]:
        if record["alias"] == alias:
            return record.get("free")
    return None


def _guard_free_mode(model_alias: str, as_json: bool) -> None:
    """Warn, or in `hard` mode refuse, before delegating to a non-free model."""
    level = modes_mod.current(sessions.current_session_id())["free"]
    if level == "off":
        return
    status = _free_status_of(model_alias)
    if status is True:
        return
    detail = "a paid metered API" if status is False else "not a confirmed zero-cost model"
    if level == "hard":
        message = (f"free mode (hard) is on and {model_alias!r} is {detail} -- "
                   "`ruti mode free soft` to allow it, or delegate to a `:free`/`free` alias")
        if as_json:
            ui.emit_json({"blocked": True, "reason": message})
        else:
            ui.bad(message)
        raise SystemExit(1)
    ui.warn(f"free mode is on and {model_alias!r} is {detail} -- proceeding anyway")


def _refuse_if_disabled(as_json: bool) -> None:
    """Exit before doing any work if `ruti off` is in effect for this session."""
    if not sessions.is_disabled(sessions.current_session_id()):
        return
    message = "ruti is OFF for this session -- run `ruti on` to resume, or do this in-session."
    if as_json:
        ui.emit_json({"disabled": True, "message": message})
    else:
        ui.bad(message)
    raise SystemExit(1)


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
    _refuse_if_disabled(as_json)
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
        ui.say(f"  [ok]{entry['score']:.2f}[/ok]  {entry['executor']}  {_executor_tags(entry)}  "
               f"[muted]{entry['command']}[/muted]")
        for reason in entry["reasons"]:
            ui.say(f"        [muted]- {ui.literal(reason)}[/muted]")

    if result["rejected"]:
        ui.heading("Ruled out")
        for entry in result["rejected"]:
            ui.say(f"  [bad]x[/bad]  {entry['executor']} {_executor_tags(entry)}: "
                   f"{ui.literal(entry['blockers'][0])}")

    ui.heading("Advice")
    ui.say(f"  {ui.literal(result['advice'])}")


def _executor_tags(entry: dict) -> str:
    """`(router: ..., metered: ...)` -- who picks the model, and who gets paid."""
    tags = []
    if entry.get("router"):
        tags.append("[warn]router: picks the model per request[/warn]")
    if entry.get("metered") is True:
        tags.append(f"[warn]metered: {ui.literal(entry['pays_in'])}[/warn]")
    elif entry.get("metered") is None:
        tags.append("[warn]price unknown[/warn]")
    elif entry["tier"] == "remote":
        tags.append("[ok]zero-cost[/ok]")
    elif entry["tier"] == "local":
        tags.append("[ok]local[/ok]")
    else:
        tags.append("[muted]subscription[/muted]")
    return "(" + ", ".join(tags) + ")"


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

    _refuse_if_disabled(as_json)

    if task_file:
        text = Path(task_file).read_text(encoding="utf-8")
    elif task:
        text = task
    else:
        raise click.ClickException("give either --task or --task-file")

    _guard_free_mode(model.split("/")[-1], as_json)

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
    _say_effective_model(outcome)
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


def _say_effective_model(outcome: delegate_mod.Outcome) -> None:
    """`pareto-code -> anthropic/claude-fable-5-1`, and what it cost if that is known."""
    alias = outcome.model_requested.split("/")[-1]
    spent = outcome.usage
    # ASCII on purpose: through a pipe Python writes the console code page, and on a
    # cp1251 console a real arrow arrives as a literal backslash escape.
    arrow = f"{alias} -> {outcome.model_effective}"
    if outcome.model_effective == usage_mod.UNKNOWN:
        ui.warn(f"  model: {ui.literal(arrow)}"
                + (f" -- {ui.literal(spent.note)}" if spent and spent.note else ""))
        return

    details = []
    if outcome.router:
        details.append("the router's pick")
    if spent and len(spent.models) > 1:
        others = ", ".join(f"{r['model']} x{r['requests']}" for r in spent.models[1:4])
        details.append(f"{spent.models[0]['requests']} of {spent.requests} requests; "
                       f"also {others}")
    elif spent:
        details.append(f"{spent.requests} request{'s' if spent.requests != 1 else ''}")
    if spent and spent.cost_usd is not None:
        details.append(f"${spent.cost_usd:.4f}"
                       + ("" if spent.cost_complete else " for the requests that stated a price"))
    elif spent and spent.requests:
        details.append("cost not stated by the provider")
    ui.say(f"  model: [head]{ui.literal(arrow)}[/head]"
           + (f"  [muted]({ui.literal('; '.join(details))})[/muted]" if details else ""))
    if spent and spent.note:
        ui.say(f"  [muted]{ui.literal(spent.note)}[/muted]")


# --------------------------------------------------------------------------- council


@main.command()
@click.argument("question")
@click.option("--models", default=None,
              help="Comma-separated proxy model names. Default: every enabled remote "
                   "provider. A resident local model can be added explicitly.")
@click.option("--timeout", type=float, default=council_mod.DEFAULT_TIMEOUT)
@click.option("--yes", is_flag=True, help="Skip the cost confirmation.")
@click.option("--json", "as_json", is_flag=True)
def council(question: str, models: str | None, timeout: float, yes: bool,
            as_json: bool) -> None:
    """Ask several models the same question and show every answer, unreconciled.

    For a genuinely hard or ambiguous call, not mechanical work -- `ruti route` is
    for that. This spends real money and real context on purpose: N parallel API
    calls, and every raw answer is meant to be read, not just the winning one.
    """
    picked = [m.strip() for m in models.split(",")] if models else council_mod.default_models()
    if not picked:
        raise click.ClickException(
            "no remote provider is registered -- `ruti provider add` one first, "
            "or pass --models explicitly"
        )

    if not as_json and not yes:
        ui.warn(f"about to ask {len(picked)} model(s) in parallel: {', '.join(picked)}")
        ui.say("[muted]each is a separate paid API call outside your Claude "
               "subscription; none of this touches the 5h window[/muted]")
        if not click.confirm("Proceed?", default=True):
            ui.say("[muted]nothing sent[/muted]")
            return

    result = council_mod.convene(question, picked, timeout=timeout)

    if as_json:
        ui.emit_json(result.summary())
        return

    for opinion in result.opinions:
        ui.heading(f"{opinion.model}  [muted]({opinion.duration_s:.1f}s)[/muted]")
        if opinion.ok:
            ui.say(ui.literal(opinion.text))
        else:
            ui.bad(ui.literal(opinion.error))
    failed = sum(1 for o in result.opinions if not o.ok)
    if failed:
        ui.say("")
        ui.warn(f"{failed} of {len(result.opinions)} model(s) did not answer")


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

    grid = ui.table("ALIAS", "MODEL", "TOOLS", "BILLING", "KEY", "VERIFIED")
    for record in registry["providers"]:
        secret = env.get(record["env_var"], "")
        grid.add_row(
            record["alias"],
            record["model"] + ("\n[warn]router: picks the model per request[/warn]"
                               if openrouter_mod.is_router_record(record) else ""),
            "[ok]yes[/ok]" if record.get("supports_tools") else "[bad]no[/bad]",
            _billing_label(record.get("free")),
            f"{record['env_var']} = {ui.mask(secret)}" if secret else "[bad]MISSING[/bad]",
            record.get("verified_at", "?"),
        )
    ui.console.print(grid)


def _billing_label(free: bool | None) -> str:
    if free is True:
        return "[ok]zero-cost[/ok]"
    if free is False:
        return "[warn]metered, USD[/warn]"
    return "[muted]unknown[/muted]"


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


# ----------------------------------------------------------------------- openrouter


@main.group()
def openrouter() -> None:
    """OpenRouter's coding routers and free models: list them, register them."""


@openrouter.command("models")
@click.option("--free/--all", "free_only", default=True,
              help="Only zero-cost models (the default).")
@click.option("--coding/--any", "coding_only", default=True,
              help="Only tool-capable models, which OpenCode requires (the default).")
@click.option("--refresh", is_flag=True, help="Bypass the 6-hour catalogue cache.")
@click.option("--json", "as_json", is_flag=True)
def openrouter_models(free_only: bool, coding_only: bool, refresh: bool, as_json: bool) -> None:
    """The recommended coding models: ruti's shortlist, plus the live catalogue."""
    catalog = openrouter_mod.fetch_catalog(force=refresh)
    rows = openrouter_mod.recommended(catalog, free_only=free_only, coding_only=coding_only)
    registered = {r["alias"] for r in providers_mod.load_registry()["providers"]}
    for row in rows:
        row["registered"] = row["alias"] in registered

    if as_json:
        ui.emit_json({"catalogue_reachable": bool(catalog), "count": len(rows), "models": rows})
        return

    if not catalog:
        ui.warn("OpenRouter's catalogue is unreachable and nothing is cached -- "
                "showing the shortlist, unverified")
    grid = ui.table("SLUG", "ALIAS", "KIND", "CTX", "TOOLS", "COST", "UPSTREAM", "REGISTERED")
    for row in rows:
        grid.add_row(
            row["id"],
            row["alias"],
            # A router's model -- and so its class and its price -- is chosen per
            # request. Saying "paid" alone would hide that the price is open-ended.
            "[warn]router[/warn]" if row["router"] else "model",
            f"{row['context_length'] // 1000}k" if row["context_length"] else "?",
            "[ok]yes[/ok]" if row["supports_tools"] else "[bad]no[/bad]",
            ("[ok]free[/ok]" if row["free"] else
             "[warn]metered, varies by pick[/warn]" if row["router"] else "metered"),
            "[ok]listed[/ok]" if row["present"] else "[bad]missing[/bad]",
            "[ok]yes[/ok]" if row["registered"] else "[muted]no[/muted]",
        )
    ui.console.print(grid)
    if any(row["router"] for row in rows):
        ui.say("[muted]a router picks the real model per request; `ruti delegate` reports "
               "which one it picked[/muted]")
    ui.say("[muted]register a set with `ruti openrouter setup`[/muted]")


@openrouter.command("setup")
@click.option("--models", "slugs_csv", default=None,
              help="Comma-separated slugs to register. Default: the shortlist, "
                   "confirmed against the catalogue.")
@click.option("--key-stdin", is_flag=True, help="Read the OpenRouter key from stdin.")
@click.option("--skip-verify", is_flag=True,
              help="Register without probing the key first (for when the proxy or "
                   "network is down).")
@click.option("--yes", is_flag=True, help="Take the defaults and do not ask before writing.")
def openrouter_setup(slugs_csv: str | None, key_stdin: bool, skip_verify: bool,
                     yes: bool) -> None:
    """Register the OpenRouter coding routers and free models as routable aliases.

    This is the 'coding harness' switch on the plumbing side: afterwards `pareto-code`
    and `free` (plus any free models you pick) are selectable through the proxy and
    OpenCode. Turn the per-session hint on separately with `ruti mode coding on`.
    """
    import os
    import sys
    import time

    provider_name = "openrouter"
    expected = providers_mod.env_var_for(provider_name)
    registry = providers_mod.load_registry()
    existing = [r for r in registry["providers"] if r["provider"] == provider_name]
    env = load_dotenv()

    # A key already on file for OpenRouter is reused rather than asked for again.
    reuse_env_var = next(
        (r["env_var"] for r in existing if env.get(r.get("env_var", ""))), None
    )
    key = ""
    if key_stdin:
        key = sys.stdin.read().strip()
    elif reuse_env_var:
        ui.say(f"[muted]reusing the OpenRouter key already in .env ({reuse_env_var})[/muted]")
    elif os.environ.get(expected):
        ui.say(f"\n[warn]{expected}[/warn] is set in your environment "
               f"({ui.mask(os.environ[expected])})")
        if click.confirm("Use it?", default=True):
            key = os.environ[expected]
    while not key and not reuse_env_var:
        key = click.prompt("OpenRouter API key", hide_input=True).strip()
        if len(key) < 12:
            ui.warn("that looks too short to be a key -- did the paste get truncated?")
            key = ""

    catalog = openrouter_mod.fetch_catalog()
    if slugs_csv:
        slugs = [s.strip() for s in slugs_csv.split(",") if s.strip()]
    else:
        slugs = []
        for row in openrouter_mod.recommended(catalog, free_only=True, coding_only=True):
            if not row["shortlisted"]:
                continue
            if not row["present"]:
                ui.say(f"[muted]skipping {row['id']} -- not in the catalogue right now[/muted]")
                continue
            if yes or click.confirm(f"register {row['id']}  ({row['alias']})?", default=True):
                slugs.append(row["id"])
    if not slugs:
        raise click.ClickException("nothing selected")

    # Probe the key once, against a slug certain to exist.
    if not skip_verify:
        probe = openrouter_mod.PARETO_CODE if openrouter_mod.PARETO_CODE in slugs else slugs[0]
        key_to_test = key or env.get(reuse_env_var or "", "")
        ui.heading(f"Testing the key against {probe}")
        verdict = providers_mod.test_key(
            provider_name, openrouter_mod.litellm_model_for(probe), key_to_test
        )
        _report(verdict)
        if not verdict.usable:
            failure = verdict.failure
            raise click.ClickException(
                f"not saved -- {failure.detail if failure else 'the key did not pass'}"
            )

    if reuse_env_var:
        env_var = reuse_env_var
    else:
        index = providers_mod.next_key_index(provider_name)
        env_var = providers_mod.ruti_env_var(provider_name, index)

    by_id = {e.get("id"): e for e in catalog}
    known_aliases = {r["alias"] for r in registry["providers"]}
    planned = []
    for slug in slugs:
        alias = openrouter_mod.alias_for(slug)
        if alias in known_aliases:
            ui.say(f"[muted]{alias} already registered -- skipping[/muted]")
            continue
        known_aliases.add(alias)
        planned.append({
            "provider": provider_name,
            "alias": alias,
            "model": openrouter_mod.litellm_model_for(slug),
            "env_var": env_var,
            "api_base": None,
            "supports_tools": True,
            "free": openrouter_mod.is_free(slug),
            "coding": openrouter_mod.is_coding(slug),
            "router": openrouter_mod.is_router(slug),
            "context_window": openrouter_mod._context(by_id.get(slug)) or None,
            "enabled": True,
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    if not planned:
        ui.ok("everything selected was already registered")
        return

    ui.heading("About to write")
    for record in planned:
        ui.say(f"  {record['alias']:<18} -> {record['model']}"
               + ("  [ok](free)[/ok]" if record["free"] else "  [muted](metered)[/muted]")
               + ("  [warn](router: picks the model per request)[/warn]"
                  if record["router"] else ""))
    if not reuse_env_var:
        ui.say(f"  litellm/.env        {env_var}=<your key>")
    if not yes and not click.confirm("Write these?", default=True):
        ui.say("[muted]nothing written[/muted]")
        return

    if not reuse_env_var:
        doctor_mod._set_env_var(env_var, key)
    registry["providers"].extend(planned)
    providers_mod.save_registry(registry)
    litellm_cfg.write_providers(providers_mod.litellm_entries(registry))
    litellm_cfg.wire_include()
    aliases = sorted(set(litellm_cfg.routable_aliases()) | {r["alias"] for r in planned})
    litellm_cfg.sync_opencode(aliases)

    ui.ok(f"registered {len(planned)} alias(es): {', '.join(r['alias'] for r in planned)}")
    ui.say("[muted]restart the proxy to route to them "
           "(`Stop-ScheduledTask`/`Start-ScheduledTask -TaskName RutiLiteLLM`), "
           "then turn the hint on with `ruti mode coding on`[/muted]")


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
