from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Database
    database_url: str = "postgresql+asyncpg://raasoa:raasoa_dev@localhost:5433/raasoa"

    # Object Storage
    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "raasoa"
    s3_secret_key: str = "raasoa_dev"
    s3_bucket: str = "raasoa-artifacts"

    # Embedding Provider
    embedding_provider: str = "ollama"
    embedding_dimensions: int = 768

    # Ollama
    ollama_base_url: str = "http://localhost:11434"
    ollama_embedding_model: str = "nomic-embed-text"

    # OpenAI
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"

    # Cohere
    cohere_api_key: str = ""
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
    ollama_chat_model: str = "qwen3:8b"

    # Reranking
    reranker: str = "passthrough"  # passthrough | ollama | cohere

    # Rate Limiting
    ingest_rate_limit_per_minute: int = 30
    retrieve_rate_limit_per_minute: int = 120

    # Authentication
    auth_enabled: bool = True
    api_keys: str = ""  # comma-separated "key:tenant_id" pairs
    webhook_secret: str = ""  # shared secret for webhook authentication
    dashboard_password: str = ""  # password for dashboard access (empty = no dashboard auth)

    # Dashboard
    dashboard_enabled: bool = True


settings = Settings()
