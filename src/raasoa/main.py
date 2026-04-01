from fastapi import FastAPI

from raasoa.api.documents import router as documents_router
from raasoa.api.health import router as health_router
from raasoa.api.ingestion import router as ingestion_router
from raasoa.api.quality import router as quality_router
from raasoa.api.retrieval import router as retrieval_router
from raasoa.dashboard.routes import router as dashboard_router

app = FastAPI(
    title="RAASOA - Enterprise RAG as a Service",
    description="Retrieval-Augmented Generation Service with Hybrid Search, "
    "Quality Gates, and Governance",
    version="0.1.0",
)

app.include_router(health_router)
app.include_router(ingestion_router)
app.include_router(retrieval_router)
app.include_router(documents_router)
app.include_router(quality_router)
app.include_router(dashboard_router)


@app.get("/")
async def root() -> dict:
    return {
        "service": "RAASOA",
        "version": "0.1.0",
        "docs": "/docs",
    }
