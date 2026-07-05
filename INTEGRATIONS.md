# Connecting RAASOA to AI Clients

RAASOA exposes your trusted knowledge to AI assistants through **two
surfaces**:

| Surface | Endpoint | Best for |
|---|---|---|
| **MCP — stdio** | `python -m raasoa.mcp.server` | local clients: Claude Desktop, Cursor |
| **MCP — HTTP** | `POST https://<host>/mcp` | cloud clients: Claude.ai, LangDock, Copilot Studio |
| **REST / OpenAPI** | `https://<host>/openapi.json` | custom actions / connectors (LangDock, Copilot) |

All 17 MCP tools (search, get-skill, dependencies, diff, ingest, …) are
available over **both** MCP transports. The REST/OpenAPI path exposes the
core actions (`searchKnowledge`, `ingestDocument`).

> **Auth.** When `AUTH_ENABLED=true`, every request needs a Bearer API
> key: `Authorization: Bearer sk-…`. Create one via
> `POST /v1/tenants/signup` or the dashboard → Account. Keep `AUTH_ENABLED=true`
> for anything reachable from the internet.

---

## 1. Claude Desktop / Claude Code (local, MCP stdio) — works today

No deployment required if you run the API locally; otherwise point
`RAASOA_URL` at your deployed host.

1. Copy `examples/claude_desktop_config.json` into your Claude Desktop
   config:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
2. Set `cwd` to your RAASOA checkout, `RAASOA_URL` to your API, and
   `RAASOA_API_KEY` to your key.
3. Restart Claude Desktop. You'll see the `raasoa` tools (a hammer icon).
4. Ask: *"Search our knowledge base for the travel expense limits."*

```json
{
  "mcpServers": {
    "raasoa": {
      "command": "uv",
      "args": ["run", "python", "-m", "raasoa.mcp.server"],
      "cwd": "/absolute/path/to/raasoa",
      "env": {
        "RAASOA_URL": "https://your-raasoa-host",
        "RAASOA_API_KEY": "sk-your-api-key"
      }
    }
  }
}
```

---

## 2. Claude.ai (web/Team/Enterprise) — remote MCP

Requires RAASOA deployed to a public HTTPS host (see `DEPLOYMENT.md`)
with `MCP_HTTP_ENABLED=true` (the default).

1. In Claude.ai → **Settings → Connectors → Add custom connector**.
2. URL: `https://<your-host>/mcp`
3. Authentication: Bearer token → your RAASOA API key.
4. Save. The RAASOA tools become available in chats and Projects.

The endpoint speaks MCP Streamable HTTP: `POST /mcp` with JSON-RPC.
A quick manual check:

```bash
curl -s https://<host>/mcp \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | jq '.result.tools[].name'
```

---

## 3. LangDock

LangDock can connect either way — pick one.

### Option A — MCP (recommended, all 17 tools)
1. LangDock → **Settings → MCP Servers → Add**.
2. Transport: HTTP. URL: `https://<your-host>/mcp`.
3. Auth header: `Authorization: Bearer sk-your-api-key`.
4. Enable the server for your assistants. RAASOA's tools now appear.

### Option B — OpenAPI custom action (fastest, core actions)
1. Generate the focused spec for your host:
   ```bash
   uv run python ops/generate_actions_openapi.py https://<your-host>
   # writes ops/openapi-actions.json
   ```
2. LangDock → **Assistant → Tools → Add custom tool → Import OpenAPI**.
3. Upload `ops/openapi-actions.json`.
4. Set authentication to Bearer with your API key.
5. The assistant gains `searchKnowledge` and `ingestDocument`.

---

## 4. Microsoft Copilot (Copilot Studio)

### Option A — MCP
1. Copilot Studio → your agent → **Tools → Add a tool → Model Context
   Protocol**.
2. Server URL: `https://<your-host>/mcp`.
3. Authentication: API key / Bearer → your RAASOA key.
4. Publish. The agent can now call RAASOA's tools.

### Option B — Custom connector (OpenAPI)
1. Generate the spec: `uv run python ops/generate_actions_openapi.py https://<your-host>`.
2. Power Platform → **Custom connectors → Import an OpenAPI file** →
   upload `ops/openapi-actions.json`.
3. Security: OAuth/API key → Bearer with your RAASOA key.
4. Add the connector to your Copilot Studio agent as an action.

> Note: this is the *query-time* integration (the agent calls RAASOA to
> retrieve). It is **not** a Microsoft Graph connector — RAASOA stays the
> source of truth and quality layer; nothing is pushed into the M365 index.

---

## Tool reference (most-used)

| Tool / Action | What it does |
|---|---|
| `raasoa_answer` / `answerQuestion` | Direct, source-cited answer. Refuses (answered=false) when sources are too weak — no hallucinations. |
| `raasoa_search` / `searchKnowledge` | Hybrid search with confidence + source attribution. Accepts `metadata_filter`, `source_type`, `agent_clearance`. |
| `raasoa_get_skill` | Fetch a SKILL document (SOP) by name, with version/ampel/owner + policy gate. |
| `raasoa_doc_dependencies` | Related documents + contradictions for a given doc. |
| `raasoa_doc_diff` | What changed between two versions of a document. |
| `raasoa_ingest` / `ingestDocument` | Add a document to the knowledge base. |

## Security notes

- The MCP HTTP endpoint and OpenAPI actions both enforce the same
  per-request Bearer key — set `AUTH_ENABLED=true` in production.
- The **MCP Policy-Gate** still applies: pass `agent_clearance`
  (`public` … `secret`) on search/skill calls; documents above the
  clearance are filtered and the denial is audit-logged
  (`/dashboard/audit`).
- Terminate TLS at your reverse proxy (Plesk / Caddy / Traefik).
