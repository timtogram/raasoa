import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.config import settings
from raasoa.db import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(session: AsyncSession = Depends(get_session)) -> dict:
    """Comprehensive health check for all service dependencies."""
    # Check DB connectivity
    try:
        result = await session.execute(text("SELECT 1"))
        db_ok = result.scalar() == 1
    except Exception:
        db_ok = False

    # Check pgvector extension
    try:
        result = await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        pgvector_version = result.scalar()
    except Exception:
        pgvector_version = None

    # Check embedding provider (Ollama)
    embedding_ok = False
    embedding_detail = "not_checked"
    if settings.embedding_provider == "ollama":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{settings.ollama_base_url}/api/tags")
                if resp.status_code == 200:
                    models = [m["name"] for m in resp.json().get("models", [])]
                    embedding_ok = any(
                        settings.ollama_embedding_model in m for m in models
                    )
                    embedding_detail = (
                        "model_available" if embedding_ok else "model_not_found"
                    )
                else:
                    embedding_detail = f"ollama_status_{resp.status_code}"
        except httpx.ConnectError:
            embedding_detail = "ollama_unreachable"
        except Exception as e:
            embedding_detail = f"error: {type(e).__name__}"
    else:
        embedding_ok = True
        embedding_detail = f"provider:{settings.embedding_provider}"

    # Check LLM for claim extraction
    llm_ok = False
    llm_detail = "not_checked"
    if settings.claim_extraction_enabled and settings.embedding_provider == "ollama":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{settings.ollama_base_url}/api/tags")
                if resp.status_code == 200:
                    models = [m["name"] for m in resp.json().get("models", [])]
                    llm_ok = any(settings.ollama_chat_model in m for m in models)
                    llm_detail = "model_available" if llm_ok else "model_not_found"
        except Exception:
            llm_detail = "ollama_unreachable"
    elif not settings.claim_extraction_enabled:
        llm_detail = "disabled"

    all_ok = db_ok and (embedding_ok or settings.embedding_provider != "ollama")

    return {
        "status": "healthy" if all_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
        "pgvector": pgvector_version,
        "embedding": {
            "provider": settings.embedding_provider,
            "status": "ok" if embedding_ok else "unavailable",
            "detail": embedding_detail,
        },
        "claim_extraction": {
            "enabled": settings.claim_extraction_enabled,
            "status": "ok" if llm_ok else "unavailable",
            "detail": llm_detail,
        },
    }


@router.get("/health/ready")
async def readiness_check(session: AsyncSession = Depends(get_session)) -> dict:
    """Lightweight readiness probe for load balancers."""
    try:
        result = await session.execute(text("SELECT 1"))
        if result.scalar() != 1:
            raise ValueError("DB check failed")
    except Exception:
        return {"ready": False}

    return {"ready": True}
