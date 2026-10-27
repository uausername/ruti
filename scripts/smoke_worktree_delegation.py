"""Explicit live worktree smoke check using only a synthetic temporary repository."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path

from ruti import delegate, ledger, worktrees


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    os.environ["RUTI_HOST"] = "codex"
    os.environ.setdefault("RUTI_SESSION_ID", "worktree-smoke-" + uuid.uuid4().hex[:12])
    source = Path(tempfile.mkdtemp(prefix="ruti-worktree-smoke-"))
    worktrees.git(source, "init", "--quiet")
    (source / "existing.txt").write_text("baseline\n", encoding="utf-8")
    worktrees.git(source, "add", ".")
    worktrees.git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                  "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "baseline")
    (source / "existing.txt").write_text("keep user staged edit\n", encoding="utf-8")
    worktrees.git(source, "add", "existing.txt")
    before = worktrees.git(source, "diff", "--cached")
    outcome = delegate.run(
        "Create only isolated.txt using a relative path, containing exactly WORKTREE_OK "
        "followed by a newline. Do not edit other files or delegate. Verify the file exists.",
        model=args.model, directory=source, timeout=args.timeout, isolated=True,
    )
    child_file = Path(outcome.worktree["directory"]) / "isolated.txt" if outcome.worktree else None
    accepted = bool(outcome.ok and child_file and child_file.is_file()
                    and child_file.read_text(encoding="utf-8").strip() == "WORKTREE_OK"
                    and outcome.files_changed == ["isolated.txt"]
                    and not (source / "isolated.txt").exists())
    imported = False
    if accepted:
        manifest = Path(outcome.worktree["patch"]).with_suffix(".json")
        imported = worktrees.apply_saved(manifest, source)
        outcome.worktree["applied"] = imported
        accepted = imported and (source / "isolated.txt").read_text().strip() == "WORKTREE_OK"
    preserved = (worktrees.git(source, "diff", "--cached") == before
                 and (source / "existing.txt").read_text() == "keep user staged edit\n")
    accepted = accepted and preserved
    ledger.record("verdict", run_id=outcome.run_id, model=args.model,
                  verdict="ok" if accepted else "bad",
                  note="live worktree smoke: checked exact result, patch import and unchanged source index")
    print(json.dumps({**outcome.summary(), "accepted": accepted, "imported": imported,
                      "original_preserved": preserved}, indent=2))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
