"""Operator automation for the Harness -> master Claude -> Codex chain.

Deterministic services that used to be done by hand by an LLM "Administrator"
session: answering safe Codex approval prompts, and (later) waking the master and
reading quota. Everything here is policy-as-data plus a thin tmux layer, so it can
be tested without a live terminal.
"""
