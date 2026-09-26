"""What the ledger already knows about each executor, so ranking stops guessing.

`router.py` has to answer a question no heuristic can: does this model actually finish
the work? It cannot, so it gives every remote executor the same `speed=0.75` and
`capability=0.7`, and every remote model then scores exactly 0.9. A tie like that is not
a judgement -- it is registration order wearing a score -- and it is the largest source
of bad advice the router gives: an alias that has failed every run it was given and a
`:free` model nobody has ever asked are neck and neck, and the tie is broken by
whichever was registered first.

The evidence is already in the ledger. `delegate` records every run with its outcome,
how long it took, how many files it changed and which model really answered, so the
history exists and goes unused. This module reads it and turns it into the two numbers
the ranking actually wants: how often an alias finishes the job, and how long it takes.

Three things it refuses to do:

* **Count a run another model answered.** A fallback that serves a request for
  `laguna-s-2.1` says nothing about `laguna-s-2.1`, and a router's pick is nobody's
  record at all -- `pareto-code` answering with a frontier model is what a router is
  for, not evidence about a model. Those runs are counted separately, so an alias whose
  only history is other models' work can say so instead of looking unproven.
* **Read "exited 0" as success.** A run that reported ok and changed no file did not do
  the job: the manager writes it again by hand and has paid for the round trip twice.
  Seen on this machine: a delegation reported ok and had changed nothing.
* **Learn from a very long past.** Sixty days, and the last twenty runs: a model that
  was rate-limited through last month's outage is not evidence about today, and twenty
  runs is enough to notice a change without one bad afternoon deciding the routing.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from . import ledger, providers, usage

# Long enough to see which alias has been reliable across a season, short enough that a
# model fixed since is not judged forever by the month it was broken.
WINDOW_DAYS = 60

# The most recent qualifying runs kept per alias. Enough to show a trend, few enough
# that a bad week ages out instead of steering every ranking from here on.
MAX_RUNS = 20

# The timeout `delegate` runs under, so a measured duration reads as a fraction of the
# worst case rather than as a number in seconds: a run that used the whole budget gets
# the floor.
TIMEOUT_S = 900.0


@dataclass
class Record:
    """What an alias's own runs say about it. A tendency, not a verdict."""

    runs: int = 0
    succeeded: int = 0
    median_s: float | None = None  # None when nothing succeeded, which is not "fast"
    excluded: int = 0  # runs that measured some other model, see `_answered_elsewhere`

    @property
    def reliability(self) -> float:
        """Successes per run, smoothed by one success and one failure.

        An alias nobody has asked scores exactly 0.5, and an alias that has run once
        cannot claim 100%. With so few runs per alias this is the only honest reading;
        the smoothing is what stops one lucky run from being a licence.
        """
        return (self.succeeded + 1) / (self.runs + 2)

    @property
    def factor(self) -> float:
        """The score multiplier, in [0.6, 1.2].

        Bounded and narrow on purpose. No history is neither a reward nor a penalty,
        and a bad record demotes an executor rather than ruling it out -- a week of
        rate limiting should change the advice, not remove the model from it. A perfect
        record is worth a fifth more than nothing, and no more than that.
        """
        return min(1.2, max(0.6, 0.6 + 0.8 * self.reliability))

    @property
    def speed(self) -> float | None:
        """Measured speed on the same 0..1 scale the fixed constants use."""
        if self.median_s is None:
            return None
        return min(0.95, max(0.3, 1 - self.median_s / TIMEOUT_S))


def records(events: list[dict[str, Any]], registry: list[dict[str, Any]],
            usage_log: list[dict[str, Any]] | None = None) -> dict[str, Record]:
    """One `Record` per executor that has delegation history, keyed as the ledger keys
    it: the model that was asked for, e.g. `ruti-router/laguna-s-2.1`.

    `registry` is `providers.load_registry()["providers"]`, needed to tell an alias's own
    runs from another's -- the alias names a model, and only the registry says which.

    `usage_log` is the proxy's per-request log (`usage.read_log()`). With it, a run is
    also checked request by request: any request in its window that another model group
    answered makes it someone else's evidence. That is the only check a router passes
    through, and the only one that sees ledger lines written before `delegate` read the
    log itself -- audited on this machine, 7 past runs had requests answered by
    gemini-flash while 6 of them were recorded as not substituted, one of them a `free`
    router run counted as that router's success.
    """
    registered = {
        str(r["alias"]): r for r in registry
        if isinstance(r, dict) and r.get("alias")
    }
    kept: dict[str, list[tuple[float, bool, float | None]]] = {}
    excluded: dict[str, int] = {}

    for event in events:
        run = _run(event)
        if run is None:
            continue
        alias, at, succeeded = run
        # The registry is keyed by the bare alias while the ledger is keyed by the
        # executor name (`ruti-router/laguna-s-2.1`) -- the same split `delegate` makes
        # when it works out which tier a run was.
        if (_answered_elsewhere(event, registered.get(_alias_of(alias)))
                or _fallback_in_log(event, alias, at, usage_log)):
            excluded[alias] = excluded.get(alias, 0) + 1
            continue
        kept.setdefault(alias, []).append((at, succeeded, _duration(event)))

    out: dict[str, Record] = {}
    # An alias whose every run was answered elsewhere still gets an entry, or `runs == 0`
    # could not be told apart from "never asked", and the two deserve different words.
    for alias in set(kept) | set(excluded):
        runs = sorted(kept.get(alias, []), key=lambda run: run[0])[-MAX_RUNS:]
        succeeded = [run for run in runs if run[1]]
        measured = sorted(run[2] for run in succeeded if run[2] is not None)
        out[alias] = Record(
            runs=len(runs),
            succeeded=len(succeeded),
            median_s=statistics.median(measured) if measured else None,
            excluded=excluded.get(alias, 0),
        )
    return out


def load() -> dict[str, Record]:
    """Every alias's track record from the recent ledger. Never raises.

    A ranking must not fail because a ledger line was odd or the registry could not be
    read: the honest answer then is no history, which leaves every candidate scoring
    exactly as it did before this module existed.
    """
    try:
        return records(
            ledger.read_events(since_days=WINDOW_DAYS),
            providers.load_registry()["providers"],
            usage.read_log(),
        )
    except Exception:
        return {}


def _fallback_in_log(event: dict[str, Any], executor: str, at: float,
                     usage_log: list[dict[str, Any]] | None) -> bool:
    """Did another model group answer any request of this run, per the proxy's log?

    The event is written when the run ends, so its window reaches back by the run's
    duration, with the same few seconds of slack `delegate` itself allows.
    """
    if not usage_log:
        return False
    alias = _alias_of(executor)
    started = at - (_duration(event) or 0.0) - 5.0
    return any(usage.served_by_fallback(entry, alias)
               for entry in usage.in_window(usage_log, alias, started, at))


def _run(event: Any) -> tuple[str, float, bool] | None:
    """The fields a delegation event must have, and whether it did the job.

    `ok` alone is not that: a run that exited cleanly and changed no file did the work
    twice over, once for the delegate and once for the manager rewriting it by hand.
    Both conditions are required, and the event is dropped entirely if either field is
    missing or the wrong type.

    The ledger is append-only and has grown fields over time, so a line can be short.
    Skipping one is right: a line that cannot say what was run is not evidence about an
    alias, and a wrong number read out of it would be applied to every ranking after.
    """
    if not isinstance(event, dict) or event.get("event") != "delegation":
        return None
    alias, at = event.get("model"), event.get("at")
    ok, files = event.get("ok"), event.get("files_changed")
    if (not isinstance(alias, str) or not alias or not _number(at)
            or not isinstance(ok, bool) or not _number(files)):
        return None
    return alias, float(at), ok and files > 0


def _duration(event: dict[str, Any]) -> float | None:
    seconds = event.get("duration_s")
    return float(seconds) if _number(seconds) else None


def _alias_of(executor: str) -> str:
    """`ruti-router/laguna-s-2.1` -> `laguna-s-2.1`, the name the registry knows."""
    return executor.rsplit("/", 1)[-1]


def _answered_elsewhere(event: dict[str, Any], registered: dict[str, Any] | None) -> bool:
    """Whether this run is evidence about some other model rather than about the alias.

    Two ways that happens, and the difference matters. `substituted` means the request
    for this alias was served by a different group entirely, so the alias did not
    answer at all; a `model_effective` naming a different model than the alias is
    registered for says the same thing more quietly. A router is exempt, because
    answering with a different model is the entire point of it.

    An alias with no registry record is trusted: nothing here knows what it was
    supposed to run, and inventing a mismatch would exclude every run of a model
    registered directly in config.yaml.
    """
    if event.get("substituted"):
        return True
    effective = event.get("model_effective")
    if not effective or registered is None or _same(effective, "unknown"):
        return False
    if event.get("router") or registered.get("router"):
        return False
    return not _same(effective, registered.get("model"))


def _same(left: Any, right: Any) -> bool:
    """Whether two model names name the same model, ignoring what is not the model:
    the provider prefix, a `:free` suffix, case.

    `openrouter/poolside/laguna-s-2.1:free` and `poolside/laguna-s-2.1:free` are one
    model reached by two routes, and the ledger holds whichever name the provider's
    response carried. `gemini/gemini-2.5-flash` where `laguna-s-2.1` was registered is
    a different model, which is the case worth catching.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    return _tail(left) == _tail(right)


def _tail(model: str) -> str:
    return model.rsplit("/", 1)[-1].lower().removesuffix(":free")


def _number(value: Any) -> bool:
    """A real number. `bool` is an `int` in Python, and `True` is not a timestamp."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)
