"""Explicit live smoke test. Sends only a tiny file-creation brief to the selected model."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ruti import delegate, ledger, proc, sessions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    os.environ["RUTI_HOST"] = "codex"
    os.environ.setdefault("RUTI_SESSION_ID", "parallel-smoke-" + uuid.uuid4().hex[:12])
    root = Path(tempfile.mkdtemp(prefix="ruti-parallel-smoke-"))
    directories = [root / "first", root / "second"]
    for directory in directories:
        directory.mkdir()
        proc.run(["git", "init", "--quiet", str(directory)]).check()

    def run(directory: Path) -> dict:
        outcome = delegate.run(
            "Create only smoke.txt with exactly PARALLEL_OK followed by a newline. "
            "Do not edit other files or delegate. Verify the file exists.",
            model=args.model, directory=directory, timeout=args.timeout,
        )
        target = directory / "smoke.txt"
        accepted = (outcome.ok and target.is_file()
                    and target.read_text(encoding="utf-8").strip() == "PARALLEL_OK"
                    and outcome.files_changed == ["smoke.txt"])
        ledger.record("verdict", run_id=outcome.run_id, model=args.model,
                      verdict="ok" if accepted else "bad",
                      note="live parallel smoke: checked exact file content and changed-file scope")
        return {**outcome.summary(), "accepted": accepted, "directory": str(directory)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, directories))
    print(json.dumps({"session": sessions.current_session_id(), "runs": outcomes}, indent=2))
    return 0 if all(o["accepted"] and o.get("usage", {}).get("requests", 0) > 0
                    for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
