"""ACL management endpoints — all tenant-scoped via auth middleware."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session
from raasoa.middleware.auth import resolve_tenant

router = APIRouter(prefix="/v1", tags=["acl"])


class AclEntryRequest(BaseModel):
    document_id: uuid.UUID
    principal_type: str  # "user", "group", "role"
    principal_id: str
    permission: str = "read"


class AclEntryResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    principal_type: str
    principal_id: str
    permission: str


@router.post("/acl", response_model=AclEntryResponse)
async def create_acl_entry(
    request: Request,
    body: AclEntryRequest,
    session: AsyncSession = Depends(get_session),
) -> AclEntryResponse:
    """Create an ACL entry (tenant-scoped)."""
    tenant_id = resolve_tenant(request)

    doc_result = await session.execute(
        text(
            "SELECT id FROM documents "
            "WHERE id = :did AND tenant_id = :tid"
        ),
        {"did": body.document_id, "tid": tenant_id},
    )
    if not doc_result.first():
        raise HTTPException(
            status_code=404, detail="Document not found"
        )

    entry_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO acl_entries "
            "(id, document_id, principal_type, principal_id, permission) "
            "VALUES (:id, :did, :ptype, :pid, :perm)"
        ),
        {
            "id": entry_id,
            "did": body.document_id,
            "ptype": body.principal_type,
            "pid": body.principal_id,
            "perm": body.permission,
        },
    )
    await session.commit()

    return AclEntryResponse(
        id=entry_id,
        document_id=body.document_id,
        principal_type=body.principal_type,
        principal_id=body.principal_id,
        permission=body.permission,
    )


@router.get(
    "/acl/{document_id}", response_model=list[AclEntryResponse]
)
async def list_acl_entries(
    request: Request,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[AclEntryResponse]:
    """List ACL entries for a document (tenant-scoped)."""
    tenant_id = resolve_tenant(request)

    # Verify document belongs to tenant
    doc_result = await session.execute(
        text(
            "SELECT id FROM documents "
            "WHERE id = :did AND tenant_id = :tid"
        ),
        {"did": document_id, "tid": tenant_id},
    )
    if not doc_result.first():
        raise HTTPException(
            status_code=404, detail="Document not found"
        )

    result = await session.execute(
        text(
            "SELECT id, document_id, principal_type, "
            "principal_id, permission "
            "FROM acl_entries WHERE document_id = :did"
        ),
        {"did": document_id},
    )
    return [
        AclEntryResponse(
            id=r.id, document_id=r.document_id,
            principal_type=r.principal_type,
            principal_id=r.principal_id,
            permission=r.permission,
        )
        for r in result.fetchall()
    ]


@router.delete("/acl/{entry_id}")
async def delete_acl_entry(
    request: Request,
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete an ACL entry (tenant-scoped)."""
    tenant_id = resolve_tenant(request)

    # Join with documents to verify tenant ownership
    result = await session.execute(
        text(
            "DELETE FROM acl_entries a "
            "USING documents d "
            "WHERE a.id = :eid "
            "AND a.document_id = d.id "
            "AND d.tenant_id = :tid "
            "RETURNING a.id"
        ),
        {"eid": entry_id, "tid": tenant_id},
    )
    if not result.first():
        raise HTTPException(
            status_code=404, detail="ACL entry not found"
        )
    await session.commit()
    return {"status": "deleted", "id": str(entry_id)}
