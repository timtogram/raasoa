from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Database
    database_url: str = "postgresql+asyncpg://raasoa:raasoa_dev@localhost:5433/raasoa"
    db_pool_size: int = 20
    db_max_overflow: int = 30

    # Embedding Provider
    #
    # embedding_dimensions MUST match the actual, fixed width of the
    # chunks.embedding pgvector column in the live database (768 by
    # default — see migration 3a8758ffa2b0_initial_schema_with_foreign_keys.py
    # and the note on raasoa.models.chunk.Chunk.embedding). This setting
    # does NOT resize the column at runtime; a real Postgres vector column
    # has a fixed dimension once created. Changing EMBEDDING_PROVIDER to
    # one with a different native dimension (e.g. "openai" -> 1536,
    # per .env.example) requires a deliberate, explicit companion
    # migration to ALTER chunks.embedding to the new width (and re-embed
    # existing rows) BEFORE this value is changed — do not just bump the
    # env var.
    embedding_provider: str = "ollama"
    embedding_dimensions: int = 768

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "nomic-embed-text"

    # OpenAI / Azure OpenAI / OpenAI-compatible
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_api_version: str = "2024-02-01"  # Azure OpenAI API version

    # Cohere
    cohere_api_key: str = ""
    cohere_base_url: str = "https://api.cohere.com"
    cohere_embedding_model: str = "embed-v4.0"

    # Chunking
    chunk_size: int = 512
    chunk_overlap: int = 80

    # Upload limits
    max_file_size_mb: int = 100

    # Quality Gates
    quality_gate_enabled: bool = True
    quality_min_text_length: int = 50
    quality_publish_threshold: float = 0.8
    quality_review_threshold: float = 0.5
    quality_max_tiny_chunk_ratio: float = 0.3
    quality_tiny_chunk_tokens: int = 20

    # Conflict Detection
    conflict_detection_enabled: bool = True
    conflict_semantic_threshold: float = 0.15
    conflict_overlap_threshold: float = 0.3

    # Claim Extraction (LLM-based)
    claim_extraction_enabled: bool = True
    claim_extraction_passes: int = 1  # 2 = multi-pass (+15-25% more claims)
    ollama_chat_model: str = "qwen3:8b"

    # LLM Judge for Conflict Resolution
    #
    # Off by default: unattended, permanent claim supersession decided by
    # a local LLM with no human in the loop is a real-stakes action even
    # scoped to a single claim (see raasoa.quality.judge). Set to true to
    # opt into auto-resolving conflicts above llm_judge_auto_resolve_threshold
    # during ingestion; judge_conflict() (get a recommendation without
    # resolving) and the manual /v1/conflicts/{id}/resolve endpoint work
    # regardless of this setting.
    llm_judge_enabled: bool = False
    llm_judge_auto_resolve_threshold: float = 0.85
    llm_judge_model: str = ""  # Empty = use ollama_chat_model

    # Reranking
    reranker: str = "passthrough"  # passthrough | ollama | cohere

    # Rate Limiting
    ingest_rate_limit_per_minute: int = 30
    retrieve_rate_limit_per_minute: int = 120

    # Authentication
    auth_enabled: bool = True
    signup_enabled: bool = True  # Allow public signup (SaaS mode)
    api_keys: str = ""  # comma-separated "key:tenant_id" pairs
    webhook_secret: str = ""  # shared secret for webhook authentication
    dashboard_password: str = ""  # password for dashboard access (empty = no dashboard auth)

    # Dashboard
    dashboard_enabled: bool = True

    # MCP — remote Streamable-HTTP transport (for LangDock, Copilot, Claude.ai)
    mcp_http_enabled: bool = True
    # Base URL the in-process MCP transport calls back into (the REST API).
    # Defaults to the local app; override if the API is reached via another host.
    mcp_internal_url: str = "http://localhost:8000"

    # HTTP
    cors_origins: str = ""  # comma-separated origins; empty = allow all for local/dev


settings = Settings()
