import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from raasoa.api.acl import router as acl_router
from raasoa.api.admin import router as admin_router
from raasoa.api.analytics import router as analytics_router
from raasoa.api.claim_clusters import router as claim_clusters_router
from raasoa.api.crm import router as crm_router
from raasoa.api.dependencies import router as dependencies_router
from raasoa.api.documents import router as documents_router
from raasoa.api.health import router as health_router
from raasoa.api.ingestion import router as ingestion_router
from raasoa.api.keys import router as keys_router
from raasoa.api.metrics import router as metrics_router
from raasoa.api.quality import router as quality_router
from raasoa.api.retrieval import router as retrieval_router
from raasoa.api.source_tree import router as source_tree_router
from raasoa.api.sources import router as sources_router
from raasoa.api.synthesis import router as synthesis_router
from raasoa.api.tenants import router as tenants_router
from raasoa.api.versioning import router as versioning_router
from raasoa.api.webhooks import router as webhooks_router
from raasoa.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

app = FastAPI(
    title="RAASOA — Knowledge Reliability Layer",
    description="Trusted retrieval with quality gates, contradiction detection, "
    "and governance for enterprise knowledge.",
    version="0.2.0",
)

def _cors_origins() -> list[str]:
    raw = settings.cors_origins.strip()
    if not raw:
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


_cors_allow_origins = _cors_origins()
# allow_credentials=True with allow_origins=["*"] makes browsers treat every
# origin as trusted for cookie-bearing requests (Starlette reflects the
# caller's Origin back verbatim in that combination) — a CSRF vector against
# the cookie-authenticated dashboard. Only send credentials when the
# deployer has explicitly configured a real origin allowlist via
# CORS_ORIGINS; the wildcard default still allows header-based (bearer
# token) API access from any origin, just not cookie-credentialed requests.
_cors_allow_credentials = _cors_allow_origins != ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(ingestion_router)
app.include_router(retrieval_router)
app.include_router(documents_router)
app.include_router(quality_router)
app.include_router(acl_router)
app.include_router(admin_router)
app.include_router(analytics_router)
app.include_router(synthesis_router)
app.include_router(sources_router)
app.include_router(keys_router)
app.include_router(tenants_router)
app.include_router(claim_clusters_router)
app.include_router(source_tree_router)
app.include_router(webhooks_router)
app.include_router(versioning_router)
app.include_router(dependencies_router)
app.include_router(crm_router)
if settings.mcp_http_enabled:
    from raasoa.mcp.http_transport import router as mcp_http_router

    app.include_router(mcp_http_router)
if settings.dashboard_enabled:
    from raasoa.dashboard.routes import router as dashboard_router

    app.include_router(dashboard_router)


@app.exception_handler(Exception)
async def global_exception_handler(
    request: Request, exc: Exception,
) -> JSONResponse:
    """Catch unhandled exceptions — log details, return safe message."""
    logging.getLogger("raasoa").exception(
        "Unhandled error on %s %s", request.method, request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "RAASOA",
        "version": "0.2.0",
        "docs": "/docs",
        "health": "/health",
        "dashboard": "/dashboard",
    }
