"""Per-child OpenCode configuration, without changing project or global files."""

from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

from .config import LMSTUDIO_BASE


def environment(run_id: str, *, local_alias: str | None = None,
                local_context: int | None = None) -> tuple[str | None, dict[str, str]]:
    config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
    if not isinstance(config, dict):
        raise ValueError("OPENCODE_CONFIG_CONTENT must contain an object")
    if local_alias is not None:
        if not re.fullmatch(r"local-[a-zA-Z0-9_-]+", local_alias):
            raise ValueError("--local-only requires a local-* alias")
        endpoint = urlsplit(LMSTUDIO_BASE)
        if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.username:
            raise ValueError("local-only requires the loopback LM Studio endpoint")
        # Dedicated provider, direct loopback endpoint, no LiteLLM fallback path.
        # This limits model routing; it is not an OS network sandbox for tools.
        model = f"ruti-local-only/{local_alias}"
        config.update({
            "enabled_providers": ["ruti-local-only"], "model": model, "small_model": model,
            "autoupdate": False, "share": "disabled",
        })
        config["provider"] = {"ruti-local-only": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": f"{LMSTUDIO_BASE}/v1", "apiKey": "not-used-locally"},
            "models": {local_alias: {"name": local_alias, **(
                {"limit": {"context": local_context, "output": min(4000, local_context // 4)}}
                if local_context else {})}},
        }}
    else:
        model = None
        provider = config.setdefault("provider", {}).setdefault("ruti-router", {})
        provider.setdefault("options", {}).setdefault("headers", {})["x-ruti-run-id"] = run_id
    return model, {"OPENCODE_CONFIG_CONTENT": json.dumps(config)}
