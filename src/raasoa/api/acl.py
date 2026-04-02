"""ACL management endpoints.

Allows setting per-document access control lists. When ACLs are set,
retrieval filters results to only return documents the querying user
has access to.
"""

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.db import get_session

router = APIRouter(prefix="/v1", tags=["acl"])


class AclEntryRequest(BaseModel):
    document_id: uuid.UUID
    principal_type: str  # "user", "group", "role"
    principal_id: str
    permission: str = "read"  # "read", "write", "admin"


class AclEntryResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    principal_type: str
    principal_id: str
    permission: str


@router.post("/acl", response_model=AclEntryResponse)
async def create_acl_entry(
    body: AclEntryRequest,
    x_tenant_id: str = Header(default="00000000-0000-0000-0000-000000000001"),
    session: AsyncSession = Depends(get_session),
) -> AclEntryResponse:
    """Create an ACL entry for a document."""
    try:
        tenant_id = uuid.UUID(x_tenant_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid tenant ID") from err

    # Verify document belongs to tenant
    doc_result = await session.execute(
        text("SELECT id FROM documents WHERE id = :did AND tenant_id = :tid"),
        {"did": body.document_id, "tid": tenant_id},
    )
    if not doc_result.first():
        raise HTTPException(status_code=404, detail="Document not found")

    entry_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO acl_entries (id, document_id, principal_type, principal_id, permission) "
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


@router.get("/acl/{document_id}", response_model=list[AclEntryResponse])
async def list_acl_entries(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[AclEntryResponse]:
    """List ACL entries for a document."""
    result = await session.execute(
        text(
            "SELECT id, document_id, principal_type, principal_id, permission "
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
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete an ACL entry."""
    result = await session.execute(
        text("DELETE FROM acl_entries WHERE id = :eid RETURNING id"),
        {"eid": entry_id},
    )
    if not result.first():
        raise HTTPException(status_code=404, detail="ACL entry not found")
    await session.commit()
    return {"status": "deleted", "id": str(entry_id)}
