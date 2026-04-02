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

    def _headers(self) -> dict[str, str]:
        return {"X-Tenant-Id": self.tenant_id}

    # --- Ingestion ---

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
                    headers=self._headers(),
                )
                response.raise_for_status()
                return IngestResult.from_dict(response.json())

    # --- Retrieval ---

    def search(self, query: str, top_k: int = 5) -> list[SearchResponse]:
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

    # --- Documents ---

    def documents(self, limit: int = 50, cursor: str | None = None) -> dict:
        """List documents with cursor-based pagination.

        Returns dict with 'items', 'next_cursor', 'has_more'.
        """
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/documents",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
            return {
                "items": [DocumentInfo.from_dict(d) for d in data.get("items", [])],
                "next_cursor": data.get("next_cursor"),
                "has_more": data.get("has_more", False),
            }

    def document(self, document_id: str) -> dict:
        """Get document details with all chunks."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/documents/{document_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    def delete_document(self, document_id: str) -> dict:
        """Soft-delete a document."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.delete(
                f"{self.base_url}/v1/documents/{document_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    # --- Quality ---

    def quality_report(self, document_id: str) -> dict:
        """Get quality report for a document."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/documents/{document_id}/quality",
            )
            response.raise_for_status()
            return response.json()

    def quality_findings(
        self, severity: str | None = None, limit: int = 50,
    ) -> list[dict]:
        """List quality findings across all documents."""
        params: dict = {"limit": limit}
        if severity:
            params["severity"] = severity
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/quality/findings",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    # --- Conflicts ---

    def conflicts(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """List conflict candidates."""
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/conflicts",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    def resolve_conflict(
        self, conflict_id: str, resolution: str, comment: str = "",
    ) -> dict:
        """Resolve a conflict (keep_a, keep_b, keep_both, reject_both)."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/conflicts/{conflict_id}/resolve",
                json={"resolution": resolution, "comment": comment},
            )
            response.raise_for_status()
            return response.json()

    # --- Reviews ---

    def reviews(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """List review tasks."""
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/reviews",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    def approve_review(self, review_id: str, comment: str = "") -> dict:
        """Approve a review task."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/reviews/{review_id}/approve",
                json={"comment": comment},
            )
            response.raise_for_status()
            return response.json()

    def reject_review(self, review_id: str, comment: str = "") -> dict:
        """Reject a review task."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/reviews/{review_id}/reject",
                json={"comment": comment},
            )
            response.raise_for_status()
            return response.json()

    # --- Health ---

    def health(self) -> dict:
        """Check service health (database, embedding, LLM)."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return response.json()
