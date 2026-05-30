"""Generate a trimmed OpenAPI spec for LLM custom-action / connector import.

LangDock and Microsoft Copilot Studio import an OpenAPI document as a
"custom action" / "connector". The full RAASOA spec has 70+ paths — far
more than an agent needs and more than some importers accept. This script
emits a focused spec containing only the agent-facing operations, with a
templated server URL and bearer-auth security.

Usage:
    uv run python ops/generate_actions_openapi.py [SERVER_URL] > ops/openapi-actions.json
    # or write in place:
    uv run python ops/generate_actions_openapi.py https://raasoa.example.com
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Operations an agent actually calls. Keyed by operationId.
KEEP_OPERATION_IDS = {
    "searchKnowledge",
    "ingestDocument",
}


def build(server_url: str) -> dict:
    from raasoa.main import app

    spec = app.openapi()

    # Keep only the whitelisted operations.
    paths: dict = {}
    for path, methods in spec.get("paths", {}).items():
        kept = {
            verb: op
            for verb, op in methods.items()
            if isinstance(op, dict) and op.get("operationId") in KEEP_OPERATION_IDS
        }
        if kept:
            paths[path] = kept

    # Collect referenced component schemas (shallow — FastAPI refs are flat
    # enough that one pass over the kept operations covers the common case;
    # we keep ALL component schemas to be safe, they're small).
    components = spec.get("components", {})

    trimmed = {
        "openapi": spec.get("openapi", "3.1.0"),
        "info": {
            "title": "RAASOA Knowledge Actions",
            "version": spec.get("info", {}).get("version", "0.2.0"),
            "description": (
                "Trusted retrieval and ingestion for enterprise knowledge. "
                "Authenticate with a Bearer API key."
            ),
        },
        "servers": [{"url": server_url}],
        "paths": paths,
        "components": {
            **components,
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "RAASOA API key as a Bearer token.",
                }
            },
        },
        "security": [{"bearerAuth": []}],
    }
    return trimmed


def main() -> None:
    server_url = sys.argv[1] if len(sys.argv) > 1 else "https://YOUR-RAASOA-HOST"
    spec = build(server_url)
    out = json.dumps(spec, indent=2, ensure_ascii=False)

    # If invoked with an explicit URL, also write the file in place.
    if len(sys.argv) > 1:
        target = Path(__file__).parent / "openapi-actions.json"
        target.write_text(out + "\n", encoding="utf-8")
        print(f"Wrote {target} ({len(spec['paths'])} paths)", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
