"""RAASOA Python Client — uses API key auth (not tenant header)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx

from raasoa_client.models import DocumentInfo, IngestResult, SearchResponse


class RAGClient:
    """Python client for the RAASOA API.

    Args:
        base_url: RAASOA API URL (e.g. http://localhost:8000)
        api_key: API key for authentication. Tenant is derived server-side.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        tenant_id: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant_id = tenant_id
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.tenant_id:
            headers["X-Tenant-Id"] = self.tenant_id
        return headers

    # --- Ingestion ---

    def ingest(self, file_path: str | Path) -> IngestResult:
        """Ingest a document file (PDF, DOCX, XLSX, PPTX, CSV, TXT, MD)."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        with open(path, "rb") as f, httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/ingest",
                files={"file": (path.name, f)},
                headers=self._headers(),
            )
            response.raise_for_status()
            return IngestResult.from_dict(response.json())

    # --- Retrieval ---

    def search(
        self, query: str, top_k: int = 5, principal_id: str | None = None,
    ) -> SearchResponse:
        """Search the knowledge base (3-layer: index → structured → hybrid)."""
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if principal_id:
            body["principal_id"] = principal_id

        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/retrieve",
                json=body,
                headers=self._headers(),
            )
            response.raise_for_status()
            return SearchResponse.from_dict(response.json())

    def feedback(
        self, query: str, chunk_id: str, document_id: str, rating: float,
    ) -> dict[str, str]:
        """Submit feedback on a search result (-1.0 to 1.0)."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/retrieve/feedback",
                json={
                    "query": query,
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "rating": rating,
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, str], response.json())

    # --- Documents ---

    def documents(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """List documents (cursor-paginated)."""
        params: dict[str, Any] = {"limit": limit}
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

    def document(self, document_id: str) -> dict[str, Any]:
        """Get document details with chunks."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/documents/{document_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def delete_document(self, document_id: str) -> dict[str, Any]:
        """Soft-delete a document."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.delete(
                f"{self.base_url}/v1/documents/{document_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    # --- Quality ---

    def quality_report(self, document_id: str) -> dict[str, Any]:
        """Get quality report for a document."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/documents/{document_id}/quality",
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def quality_findings(
        self, severity: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List quality findings across all documents."""
        params: dict[str, Any] = {"limit": limit}
        if severity:
            params["severity"] = severity
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/quality/findings",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(list[dict[str, Any]], response.json())

    def conflicts(
        self, status: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List conflict candidates."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/conflicts",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(list[dict[str, Any]], response.json())

    def resolve_conflict(
        self, conflict_id: str, resolution: str, comment: str = "",
    ) -> dict[str, Any]:
        """Resolve a conflict (keep_a, keep_b, keep_both, reject_both)."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/conflicts/{conflict_id}/resolve",
                json={"resolution": resolution, "comment": comment},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    # --- Reviews ---

    def reviews(
        self, status: str | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List review tasks."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/reviews",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(list[dict[str, Any]], response.json())

    def approve_review(self, review_id: str, comment: str = "") -> dict[str, Any]:
        """Approve a review task."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/reviews/{review_id}/approve",
                json={"comment": comment},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def reject_review(self, review_id: str, comment: str = "") -> dict[str, Any]:
        """Reject a review task."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/reviews/{review_id}/reject",
                json={"comment": comment},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    # --- Knowledge Compilation ---

    def synthesis(self, topic: str | None = None) -> Any:
        """Get topic synthesis (or list all)."""
        with httpx.Client(timeout=self.timeout) as client:
            if topic:
                response = client.get(
                    f"{self.base_url}/v1/synthesis/{topic}",
                    headers=self._headers(),
                )
            else:
                response = client.get(
                    f"{self.base_url}/v1/synthesis",
                    headers=self._headers(),
                )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def compile(self, topic: str | None = None) -> dict[str, Any]:
        """Trigger knowledge compilation."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/synthesis/compile",
                json={"topic": topic},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def curate(self) -> dict[str, Any]:
        """Run the LLM curation pipeline (normalize + index + lint)."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/synthesis/curate",
                json={},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    # --- Answer (cited, honest-refusal) ---

    def answer(
        self, query: str, top_k: int = 6, min_confidence: float = 0.3,
    ) -> dict[str, Any]:
        """Answer a question with inline citations, or refuse honestly."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/answer",
                json={"query": query, "top_k": top_k, "min_confidence": min_confidence},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    # --- Sources (connectors) ---

    def sources(self) -> list[dict[str, Any]]:
        """List configured data sources."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/v1/sources", headers=self._headers())
            response.raise_for_status()
            return cast(list[dict[str, Any]], response.json())

    def connect_source(
        self,
        source_type: str,
        name: str,
        config: dict[str, Any] | None = None,
        auto_index: bool = True,
        sync_query: str = "*",
        sync_limit: int = 50,
        default_visibility: str | None = None,
    ) -> dict[str, Any]:
        """Connect a new source. With auto_index=True (default), also runs
        an immediate sync and returns a data-quality/conflict snapshot."""
        body: dict[str, Any] = {
            "source_type": source_type,
            "name": name,
            "config": config or {},
            "auto_index": auto_index,
            "sync_query": sync_query,
            "sync_limit": sync_limit,
        }
        if default_visibility is not None:
            body["default_visibility"] = default_visibility
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/sources", json=body, headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def sync_source(
        self, source_id: str, query: str = "*", limit: int = 50,
    ) -> dict[str, Any]:
        """Trigger a sync for an existing source."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/sources/{source_id}/sync",
                json={"query": query, "limit": limit},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def set_source_visibility(
        self,
        source_id: str,
        default_visibility: str,
        grant_principal_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Admin-only: set a source's default visibility and optionally
        grant whole-source read access to one or more principals."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.patch(
                f"{self.base_url}/v1/sources/{source_id}/visibility",
                json={
                    "default_visibility": default_visibility,
                    "grant_principal_ids": grant_principal_ids or [],
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    # --- Admin: tenant opt-in, groups/memberships, personal keys ---

    def admin_enable(self) -> dict[str, Any]:
        """Turn on the Admin API for the caller's tenant. Requires the
        tenant's own master API key."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(f"{self.base_url}/v1/admin/enable", headers=self._headers())
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def admin_status(self) -> dict[str, Any]:
        """Check whether the Admin API is enabled and whether the caller
        is admin-capable."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/v1/admin/status", headers=self._headers())
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def groups(self) -> list[dict[str, Any]]:
        """List principal groups."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/v1/admin/groups", headers=self._headers())
            response.raise_for_status()
            return cast(list[dict[str, Any]], response.json())

    def create_group(self, principal_id: str, display_name: str | None = None) -> dict[str, Any]:
        """Create a principal group, e.g. principal_id='group:sales'."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/admin/groups",
                json={"principal_id": principal_id, "display_name": display_name},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def delete_group(self, group_principal_id: str) -> dict[str, Any]:
        """Delete a group and its memberships."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.delete(
                f"{self.base_url}/v1/admin/groups/{group_principal_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def group_members(self, group_principal_id: str) -> list[dict[str, Any]]:
        """List a group's members."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/admin/groups/{group_principal_id}/members",
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(list[dict[str, Any]], response.json())

    def add_group_member(
        self, group_principal_id: str, member_principal_id: str,
    ) -> dict[str, Any]:
        """Add a member (a user or another group) to a group."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/admin/groups/{group_principal_id}/members",
                json={"member_principal_id": member_principal_id},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def remove_group_member(
        self, group_principal_id: str, member_principal_id: str,
    ) -> dict[str, Any]:
        """Remove a member from a group."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.delete(
                f"{self.base_url}/v1/admin/groups/{group_principal_id}/members/"
                f"{member_principal_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def create_user_key(
        self,
        name: str,
        principal_id: str,
        clearance: str = "public",
        is_admin: bool = False,
    ) -> dict[str, Any]:
        """Issue a personal API key bound to a principal_id, e.g.
        principal_id='user:jane'. The full key is only returned here —
        it can't be retrieved again afterwards."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/admin/keys",
                json={
                    "name": name, "principal_id": principal_id,
                    "clearance": clearance, "is_admin": is_admin,
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    def user_keys(self) -> list[dict[str, Any]]:
        """List API keys with their principal_id/clearance/is_admin."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/v1/admin/keys", headers=self._headers())
            response.raise_for_status()
            return cast(list[dict[str, Any]], response.json())

    def effective_access(self, principal_id: str) -> dict[str, Any]:
        """Source-level visibility matrix for a given principal."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{self.base_url}/v1/admin/effective-access",
                params={"principal_id": principal_id},
                headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    # --- CRM (structured query path) ---

    def crm_query(
        self,
        object_type: str,
        filters: list[dict[str, Any]] | None = None,
        sort: dict[str, str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Filter typed CRM records (deals/contacts/companies/tickets) via
        the whitelisted filter DSL — not free-text search."""
        body: dict[str, Any] = {"object_type": object_type, "limit": limit}
        if filters:
            body["filters"] = filters
        if sort:
            body["sort"] = sort
        with httpx.Client(timeout=self.timeout) as client:
            response = client.post(
                f"{self.base_url}/v1/crm/query", json=body, headers=self._headers(),
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

    # --- Health ---

    def health(self) -> dict[str, Any]:
        """Check service health."""
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}/health")
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
