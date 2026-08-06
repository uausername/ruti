"""ruti -- keep Claude Code as the manager, route the typing to whatever is cheapest.

Claude Code stays on its subscription and untouched: no proxy in front of it, no
environment overrides, no lost session. It just gains tooling to see what executors
exist, what they cost, and how much budget is left -- then delegates accordingly.
"""

__version__ = "2.0.0"
