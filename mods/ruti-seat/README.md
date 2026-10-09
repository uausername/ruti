# ruti-seat

A Claude Code mod that lets the session change its own model and effort on `ruti manager`'s advice.

- On each fresh prompt it asks `ruti seat plan` (classifies the prompt, applies the policy in `ruti/auto_seat.py`),
  sets the model at once (`$.config.set`, allowed inside the prompt hook) and the effort when the turn ends
  (`/effort` is refused while the hook holds the turn).
- `ruti seat mode shadow` (default) only journals what it would do (`auto-seat.jsonl` in ruti's state dir);
  `ruti seat mode on` applies it; `off` does neither.
- Load it with `claude --plugin-dir <path to this folder>`, or from a hot-reloaded mods folder.
