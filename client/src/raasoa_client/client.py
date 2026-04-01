from __future__ import annotations

from pathlib import Path

import httpx

from raasoa_client.models import DocumentInfo, IngestResult, SearchResponse


DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


class RAGClient:
    """Python client for the RAASOA API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        tenant_id: str = DEFAULT_TENANT,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.timeout = timeout

    def ingest(self, file_path: str | Path) -> IngestResult:
        """Ingest a document file (PDF, DOCX, TXT, MD)."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        with open(path, "rb") as f:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/v1/ingest",
                    files={"file": (path.name, f)},
                    headers={"X-Tenant-Id": self.tenant_id},
                )
                response.raise_for_status()
                return IngestResult.from_dict(response.json())

    def search(
        self, query: str, top_k: int = 5
    ) -> list[SearchResponse]:
        """Search for relevant chunks using hybrid search."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/retrieve",
                json={
                    "query": query,
                    "tenant_id": self.tenant_id,
                    "top_k": top_k,
                },
            )
            response.raise_for_status()
            data = response.json()
            return SearchResponse.from_dict(data)

    def documents(self, limit: int = 50) -> list[DocumentInfo]:
        """List all ingested documents."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/documents",
                params={"limit": limit},
                headers={"X-Tenant-Id": self.tenant_id},
            )
            response.raise_for_status()
            return [DocumentInfo.from_dict(d) for d in response.json()]

    def document(self, document_id: str) -> dict:
        """Get document details with all chunks."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/documents/{document_id}",
            )
            response.raise_for_status()
            return response.json()

    def health(self) -> dict:
        """Check service health."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()
