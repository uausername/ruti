"""Close the bracket on a session's quota reading.

Paired with the marker `session_start` writes, this is what lets `ruti report` say how
much of the window a session consumed. On its own that number proves nothing -- other
work shares the same window, and a session spanning a reset shows utilisation falling.
It becomes informative only in aggregate, compared between sessions that delegated and
sessions that did not, which is why it is worth accumulating from the beginning.
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        from ruti import ledger, quota

        snapshot = quota.load()
        ledger.record(
            "session_end",
            five_hour=snapshot.five_hour.used_percentage if snapshot.five_hour else None,
            seven_day=snapshot.seven_day.used_percentage if snapshot.seven_day else None,
            band=snapshot.band,
            freshness=snapshot.freshness,
        )
    except Exception:
        # A session ending must never be blocked by bookkeeping.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
