# Codex Ruti footer patch

`statusline-ruti-0.164.0.patch` applies to the official `openai/codex` tag
`rust-v0.164.0`, commit `6aaac694803004a63c6f393d153c96d22521a6b9`.
It adds an optional `ruti` status-line item and its local, bounded Python adapter.
The client package version stays 0.164.0 for compatibility with that release's
existing app-server daemon.

Upstream source is distributed under Apache-2.0; its license and notices are
preserved in `CODEX-LICENSE` and `CODEX-NOTICE`.

Build and installation instructions: [Codex integration](../../docs/codex.md#ruti-inside-the-codex-cli-status-line).
