"""Choosing who does the work.

The split of labour is deliberate. Claude classifies the task -- how big, what kind,
whether it needs the repository in context, whether it is security-sensitive. That is
a judgement call, and pretending a Python heuristic makes it better would be a lie.
This module supplies the things Claude cannot see -- what is loaded, what fits, what
has been verified, how much subscription budget is left -- and applies the policy.

Two rules keep it honest:

* **Hard gates before scores.** A model that cannot emit structured tool calls cannot
  drive `opencode`, whatever else it has going for it. A window smaller than the
  prompt is not a slower option, it is a failing one.
* **Delegation is not always right.** Below a size threshold the round trip costs more
  turns than writing the code inline, and orchestration turns are billed to the same
  budget delegation is meant to protect. The router says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from . import litellm_cfg, lmstudio, planner, providers, quota, vram

# Measured, not guessed: `opencode run` sends an 8095-token system prompt before any
# task text. A model whose window cannot hold it will not run slowly, it will fail --
# and on the way it may claim to have written files it never touched.
OPENCODE_PROMPT_TOKENS = 8095

# Rough conversion for sizing a task. Deliberately generous: under-estimating the
# context puts work on a model that cannot hold it.
TOKENS_PER_LINE = 14
TOKENS_PER_FILE = 400
RESPONSE_HEADROOM = 4000

# Below this, orchestration costs more than it saves.
TRIVIAL_LOC = 40
TRIVIAL_FILES = 2

Kind = Literal["boilerplate", "implement", "refactor", "debug", "analyze", "review", "security"]

# Kinds that need judgement about code the delegate cannot see, or that carry
# consequences a cheap model should not be trusted with.
KEEP_IN_HOUSE = {"security", "review"}


@dataclass
class Candidate:
    name: str
    tier: str  # "local" | "remote" | "anthropic" | "self"
    context_window: int
    supports_tools: bool
    quota_cost: float  # 0 = free, 1 = burns the subscription window hardest
    speed: float  # 0..1, higher is faster to a finished answer
    capability: float  # 0..1, rough ceiling on task difficulty it can handle
    command: str = ""
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def eligible(self) -> bool:
        return not self.blockers


@dataclass
class Task:
    kind: str = "implement"
    files: int = 1
    loc: int = 50
    needs_tools: bool = True
    repo_context: str = "small"  # none | small | large
    risk: str = "low"  # low | medium | high
    latency: str = "background"  # interactive | background

    @property
    def estimated_tokens(self) -> int:
        base = self.loc * TOKENS_PER_LINE + self.files * TOKENS_PER_FILE
        multiplier = {"none": 1.0, "small": 1.6, "large": 3.0}.get(self.repo_context, 1.6)
        return int(base * multiplier) + RESPONSE_HEADROOM

    @property
    def difficulty(self) -> float:
        by_kind = {
            "boilerplate": 0.2, "implement": 0.5, "refactor": 0.45,
            "debug": 0.8, "analyze": 0.7, "review": 0.8, "security": 0.95,
        }
        score = by_kind.get(self.kind, 0.5)
        if self.files > 8:
            score += 0.1
        if self.repo_context == "large":
            score += 0.15
        if self.risk == "high":
            score += 0.2
        return min(1.0, score)

    @property
    def trivial(self) -> bool:
        return self.loc <= TRIVIAL_LOC and self.files <= TRIVIAL_FILES


def _local_candidates(task: Task) -> list[Candidate]:
    out: list[Candidate] = []
    if not lmstudio.available():
        return out

    server_up = lmstudio.server_running()
    loaded = {m.key: m for m in lmstudio.loaded_models()} if server_up else {}
    gpu = vram.primary_gpu()
    idle_budget = (
        vram.budget_mib(gpu) + sum(planner.footprint(m) for m in loaded.values())
        if gpu else 0
    )

    for model in lmstudio.list_models():
        if model.kind != "llm":
            continue
        identifier = planner.identifier_for(model.key)
        resident = loaded.get(model.key)
        fit = vram.largest_fitting_context(model, idle_budget)
        window = (resident.loaded_context if resident else None) or (fit[0] if fit else 0)

        candidate = Candidate(
            name=f"ruti-router/{identifier}",
            tier="local",
            context_window=window,
            supports_tools=model.tool_use,
            quota_cost=0.0,
            speed=0.55 if resident else 0.4,  # a swap costs several seconds
            capability=0.35,  # small local models; honest about the ceiling
            command=f"ruti delegate --model {identifier} --task-file <file>",
        )
        candidate.reasons.append("runs on this machine: no subscription cost, no data leaves")
        if resident:
            candidate.reasons.append(f"already resident at {window} tokens")
        elif fit:
            candidate.reasons.append(f"would load at {window} tokens (a few seconds)")

        if not server_up:
            candidate.blockers.append("the LM Studio server is down -- `ruti doctor --fix`")
        if not model.tool_use:
            candidate.blockers.append("cannot emit structured tool calls, so `opencode` cannot use it")
        if not window:
            candidate.blockers.append("does not fit in this GPU's memory at any context")
        out.append(candidate)
    return out


def _remote_candidates(task: Task) -> list[Candidate]:
    out: list[Candidate] = []
    served = set(litellm_cfg.served_models())

    seen: set[str] = set()
    for record in providers.load_registry()["providers"]:
        alias = record["alias"]
        if alias in seen or not record.get("enabled", True):
            continue
        seen.add(alias)
        candidate = Candidate(
            name=f"ruti-router/{alias}",
            tier="remote",
            context_window=record.get("context_window") or 1_000_000,
            supports_tools=bool(record.get("supports_tools")),
            quota_cost=0.0,
            speed=0.75,
            capability=0.7,
            command=f"ruti delegate --model {alias} --task-file <file>",
            reasons=[f"{record['model']} -- costs no subscription quota"],
        )
        if alias not in served:
            candidate.blockers.append("registered but not served -- restart the proxy")
        if not record.get("supports_tools"):
            candidate.blockers.append("no verified tool calling, so it cannot drive `opencode`")
        out.append(candidate)

    # Anything configured directly in config.yaml, which the registry does not know
    # about, is still routable and should not be invisible.
    for alias in sorted(served):
        if alias.startswith("local-") or alias in seen:
            continue
        out.append(Candidate(
            name=f"ruti-router/{alias}",
            tier="remote",
            context_window=1_000_000,
            supports_tools=True,
            quota_cost=0.0,
            speed=0.75,
            capability=0.7,
            command=f"ruti delegate --model {alias} --task-file <file>",
            reasons=["configured in config.yaml; costs no subscription quota"],
        ))
    return out


def _anthropic_candidates(task: Task, snapshot: quota.Quota) -> list[Candidate]:
    allowed = set(snapshot.policy["anthropic_executors"])
    specs = [
        ("haiku", 0.12, 0.85, 0.45, "low"),
        ("sonnet", 0.35, 0.7, 0.8, "medium"),
    ]
    out = []
    for model, cost, speed, capability, effort in specs:
        candidate = Candidate(
            name=f"claude:{model}",
            tier="anthropic",
            context_window=200_000,
            supports_tools=True,
            quota_cost=cost,
            speed=speed,
            capability=capability,
            command=f"spawn a subagent with model={model}, effort={effort}",
            reasons=[
                "its context stays out of the manager's window, which is what actually "
                "drives the burn rate",
            ],
        )
        if model not in allowed:
            candidate.blockers.append(
                f"the {snapshot.band} band does not permit Anthropic {model} executors"
            )
        out.append(candidate)
    return out


def _self_candidate(task: Task, snapshot: quota.Quota) -> Candidate:
    candidate = Candidate(
        name="claude:self",
        tier="self",
        context_window=200_000,
        supports_tools=True,
        quota_cost=1.0,
        speed=0.95,
        capability=1.0,
        command="write it in this session",
        reasons=["no orchestration overhead, full repository context already loaded"],
    )
    if snapshot.band in (quota.RED, quota.CRITICAL):
        candidate.blockers.append(
            f"the {snapshot.band} band forbids spending the manager's own budget on implementation"
        )
    return candidate


def rank(task: Task, snapshot: quota.Quota | None = None) -> dict[str, Any]:
    snapshot = snapshot or quota.load()
    needed = task.estimated_tokens + (OPENCODE_PROMPT_TOKENS if task.needs_tools else 0)

    candidates = (
        _local_candidates(task)
        + _remote_candidates(task)
        + _anthropic_candidates(task, snapshot)
        + [_self_candidate(task, snapshot)]
    )

    for candidate in candidates:
        # Hard gates.
        if task.needs_tools and not candidate.supports_tools:
            candidate.blockers.append("the task requires tool calls")
        if candidate.context_window and candidate.context_window < needed:
            candidate.blockers.append(
                f"window {candidate.context_window} < the ~{needed} tokens this needs"
                + (f" ({OPENCODE_PROMPT_TOKENS} of which is opencode's own prompt)"
                   if task.needs_tools and candidate.tier in ("local", "remote") else "")
            )
        if candidate.capability < task.difficulty and candidate.tier != "self":
            candidate.blockers.append(
                f"below the difficulty this task looks like ({task.difficulty:.2f})"
            )
        if task.kind in KEEP_IN_HOUSE and candidate.tier in ("local", "remote"):
            candidate.blockers.append(
                f"{task.kind} work is not delegated off the subscription -- it needs "
                "judgement about code the delegate cannot see"
            )

        # Score. Budget dominates, then speed; capability is already gated above.
        budget_term = 1.0 - candidate.quota_cost * (0.4 + 0.6 * _pressure(snapshot))
        candidate.score = round(budget_term * (0.6 + 0.4 * candidate.speed), 3)

    if task.trivial:
        # Every executor except the session itself carries a round trip: writing the
        # brief, waiting, reading the summary, reviewing the diff. Those turns are
        # billed to the same budget delegation exists to protect, and below a certain
        # size they cost more than the generated tokens would have. This applies to a
        # cheap Anthropic subagent just as much as to an external one.
        for candidate in candidates:
            if candidate.tier != "self":
                candidate.score *= 0.35
                candidate.reasons.append(
                    "a task this small costs more in orchestration turns than it saves"
                )

    eligible = sorted(
        (c for c in candidates if c.eligible), key=lambda c: c.score, reverse=True
    )
    rejected = [c for c in candidates if not c.eligible]

    return {
        "quota": {
            "band": snapshot.band,
            "freshness": snapshot.freshness,
            "age_seconds": round(snapshot.age_seconds),
            "five_hour_used": snapshot.five_hour.used_percentage if snapshot.five_hour else None,
            "projected_at_reset": snapshot.projected_at_reset,
            "guidance": snapshot.policy["guidance"],
        },
        "task": {
            "kind": task.kind, "files": task.files, "loc": task.loc,
            "estimated_tokens": needed, "difficulty": round(task.difficulty, 2),
            "trivial": task.trivial,
        },
        "ranked": [_render(c) for c in eligible],
        "rejected": [_render(c) for c in rejected],
        "advice": _advice(task, snapshot, eligible),
    }


def _pressure(snapshot: quota.Quota) -> float:
    """0 when the window is untouched, 1 when it is nearly spent."""
    order = [quota.GREEN, quota.YELLOW, quota.ORANGE, quota.RED, quota.CRITICAL]
    if snapshot.band == quota.UNKNOWN:
        return 0.6  # unknown is treated as pressured, not as free
    return order.index(snapshot.band) / (len(order) - 1)


def _render(candidate: Candidate) -> dict[str, Any]:
    return {
        "executor": candidate.name,
        "tier": candidate.tier,
        "score": candidate.score,
        "context_window": candidate.context_window,
        "command": candidate.command,
        "reasons": candidate.reasons,
        "blockers": candidate.blockers,
    }


def _advice(task: Task, snapshot: quota.Quota, eligible: list[Candidate]) -> str:
    if not eligible:
        return (
            "Nothing is eligible. Fix what the blockers name -- most often `ruti doctor "
            "--fix` for a stopped service, or a model whose window is too small."
        )
    best = eligible[0]
    if best.tier == "self":
        if task.kind in KEEP_IN_HOUSE:
            return (
                f"Do it in session. {task.kind} work is not delegated regardless of "
                "budget -- it turns on context and consequences a delegate cannot weigh."
            )
        if task.trivial:
            return "Do it in session; delegating this would cost more turns than it saves."
        if snapshot.band in (quota.RED, quota.CRITICAL):
            return (
                f"Only the session itself is eligible, but the band is {snapshot.band}. "
                "Prefer writing a handoff over starting this now."
            )
        return (
            "Do it in session -- no delegate cleared the bar for this one. "
            "See the ruled-out list for why."
        )
    if snapshot.freshness in ("stale", "unknown", "never"):
        return (
            f"Use {best.name}. The quota reading is {snapshot.freshness}, so this "
            "assumes the window is more spent than it may be -- open a Claude Code TUI "
            "to refresh it."
        )
    return f"Use {best.name}. {snapshot.policy['guidance']}"
