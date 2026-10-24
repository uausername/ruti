# ruti-seat

A Claude Code mod that lets the session change its own model and effort on `ruti manager`'s advice.

- On each fresh prompt it asks `ruti seat plan` (classifies the prompt, applies the policy in `ruti/auto_seat.py`),
  sets the model at once (`$.config.set`, allowed inside the prompt hook) and the effort when the turn ends
  (`/effort` is refused while the hook holds the turn).
- `$.config.set` and `/effort` save what they set as the default for new sessions; the mod puts `model` and
  `modelSettings` in `~/.claude/settings.json` back as they were after each switch, so a seat picked for one
  session's task does not become the next session's. The running session keeps its new seat.
- The model switch shows from the turn after the prompt that asked for it.
- `ruti seat mode on` (default) applies the plan; `shadow` only journals what it would do; `off` does neither.
  Every plan is journalled to `auto-seat.jsonl` in ruti's state dir unless the mode is `off`.
- Load it with `claude --plugin-dir <path to this folder>`, or from a hot-reloaded mods folder.
- After a flow handoff (`ruti flow handoff`) it closes the old session with `/exit` once `ruti flow may-close`
  says the next one is launched -- unless the Stop hook saw background tasks still running there (a keep-alive,
  a server), which `/exit` would kill: then the window stays open and a toast names them.
