# RAASOA — Knowledge Reliability Layer

**Trusted retrieval for enterprise AI. Because hallucinations are a data problem, not a model problem.**

RAASOA sits between your source systems and your AI agents. When your documents disagree — about budgets, policies, who owns what — RAASOA knows, flags it, and decides. Your agents only see knowledge that's been verified.

```
  Your Sources          RAASOA                  Your AI
  ────────────    ──────────────────    ──────────────────
  SharePoint ──►│                  │   │  Claude        │
  Jira       ──►│  Knowledge       │   │  Cursor        │
  Notion     ──►│  Reliability     │──►│  Internal bots │
  Files      ──►│  Layer           │   │  Your agents   │
                └──────────────────┘   └──────────────────┘
```

## Why This Exists

Your agents read documents and make decisions. But your documents lie to them.

- Three policies say three different things about remote work
- The 2024 pricing sheet contradicts the 2026 one — both are indexed
- A scanned PDF was parsed wrong and the agent quotes garbage as fact
- Nobody knows which document is actually current

Traditional RAG returns the "most similar" chunk. That's not the same as returning the *correct* one.

## What You Get

| | |
|---|---|
| **Know your data quality** | Every document gets a quality score. Half-parsed PDFs, empty templates, and junk content get quarantined — they never reach your agents. |
| **See contradictions automatically** | When documents disagree, RAASOA flags it before your agent cites the wrong one. |
| **Resolve conflicts intelligently** | AI evaluates each conflict and auto-resolves the easy ones. Hard cases go to a human. |
| **Retrieval you can trust** | Every search result shows where it came from: which document, which page, which source. Your users can verify. |
| **Measurable quality** | Built-in evaluation framework. Prove your retrieval is getting better, don't guess. |

## Who This Is For

- **Enterprise teams** drowning in conflicting policies, outdated docs, and unverified information
- **AI product builders** who need their agent to cite verified facts, not hallucinated ones
- **Compliance-conscious orgs** that need audit trails for what their AI read and decided

## Getting Started

```bash
git clone https://github.com/timtogram/raasoa.git && cd raasoa
cp .env.example .env
docker compose up -d
```

Dashboard: `http://localhost:8000/dashboard`

Upload a document. See its quality score. Upload a contradicting one. Watch RAASOA catch it.

## Supported Formats

PDF, DOCX, XLSX, PPTX, CSV, HTML, TXT, Markdown. Tables preserved. Page numbers tracked. Sheet names recorded.

## Source Connectors

- **Notion** — native, recursive page-block sync with delta filtering
- **SharePoint** — native via Microsoft Graph drive delta, folder paths, deletes
- **Jira** — native via Atlassian Cloud JQL search
- **Confluence** — webhook-based
- **Custom** — any system with HTTP access

## For AI Agents

RAASOA includes an MCP server. Connect Claude, Cursor, or any MCP-compatible agent:

```json
{
  "mcpServers": {
    "raasoa": {
      "command": "uv",
      "args": ["run", "python", "-m", "raasoa.mcp"],
      "env": { "RAASOA_URL": "http://localhost:8000" }
    }
  }
}
```

Your agent now has 11 tools to search, evaluate, and reason about your knowledge — with source provenance on every result.

## REST API

Full OpenAPI docs at `/docs` once running. Key endpoints:

```
POST /v1/ingest              # Upload documents
POST /v1/retrieve            # Search with provenance
GET  /v1/conflicts           # See contradictions
POST /v1/conflicts/auto-resolve  # Let AI decide the easy ones
GET  /v1/documents/{id}/quality  # Quality report
```

Python SDK available: `pip install raasoa-client`

## Configuration

Default setup is local-first: Ollama + PostgreSQL, no API keys needed, no data leaves your machine.

For production:

```env
EMBEDDING_PROVIDER=openai      # or azure-openai, cohere, ollama
AUTH_ENABLED=true
API_KEYS=sk-your-key:your-tenant-uuid
CORS_ORIGINS=https://your-app.example.com
SIGNUP_ENABLED=false
```

Full config reference in `.env.example`.

## Architecture

PostgreSQL + pgvector + tsvector. No Redis, no Elasticsearch, no separate vector database. One service, one database, one thing to operate.

## Development

```bash
uv sync --extra dev --extra parsing
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn raasoa.main:app --reload --port 8000
uv run pytest -v
```

## License

[Business Source License 1.1](LICENSE) — free for self-hosted use, including commercial use within your organization. Offering RAASOA as a hosted service to third parties requires a commercial license. Converts to Apache 2.0 on 2030-04-14.

Commercial licensing: licensing@mesakumo.com

---

RAASOA is built by [Mesakumo](https://mesakumo.com).
