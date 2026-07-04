"""Structured CRM query path — a whitelisted filter DSL over crm_objects.

No raw SQL, and no free-text query, ever comes from the caller: every
field name and every value is bound as a SQL parameter (never spliced
into the query text), and the only way caller input selects *which* SQL
operator is used is a Python-side dict lookup keyed on an already-Pydantic
-validated ``op`` string. This is the "sichere Filter-DSL, kein
Freitext-SQL" requirement from the design review (Task #15) — the same
"always bind, never splice" discipline as ``acl_predicate_sql`` and the
JSONB ``metadata_filter`` fix in ``hybrid_search.py``.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from raasoa.security.principal import crm_acl_predicate_sql

_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMERIC_RE = r"^-?[0-9]+(\.[0-9]+)?$"

_OPERATORS = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "contains": "ILIKE",
    "in": None,  # handled specially — ANY(:param)
    "is_null": None,  # handled specially — no value/operator needed
}
_NUMERIC_OPS = {"gt", "gte", "lt", "lte"}

CRM_OBJECT_TYPES = ("deals", "contacts", "companies", "tickets")


class CrmFilter(BaseModel):
    field: str
    op: str
    value: Any = None

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        if not _FIELD_RE.match(v):
            raise ValueError(
                f"Invalid field name {v!r} — must match ^[A-Za-z_][A-Za-z0-9_]*$",
            )
        return v

    @field_validator("op")
    @classmethod
    def _validate_op(cls, v: str) -> str:
        if v not in _OPERATORS:
            raise ValueError(f"Unsupported operator {v!r}. Choose from: {sorted(_OPERATORS)}")
        return v

    @model_validator(mode="after")
    def _validate_value_shape(self) -> CrmFilter:
        if self.op == "is_null":
            return self
        if self.op == "in":
            if not isinstance(self.value, list) or not self.value:
                raise ValueError("op 'in' requires a non-empty list value")
            return self
        if self.value is None:
            raise ValueError(f"op {self.op!r} requires a non-null value")
        if self.op in _NUMERIC_OPS and not isinstance(self.value, int | float):
            raise ValueError(f"op {self.op!r} requires a numeric value")
        return self


class CrmSort(BaseModel):
    field: str
    direction: str = "asc"

    @field_validator("field")
    @classmethod
    def _validate_field(cls, v: str) -> str:
        if not _FIELD_RE.match(v):
            raise ValueError(f"Invalid field name {v!r}")
        return v

    @field_validator("direction")
    @classmethod
    def _validate_direction(cls, v: str) -> str:
        if v not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        return v


class CrmQuery(BaseModel):
    object_type: str = Field(..., description="deals, contacts, companies, or tickets")
    filters: list[CrmFilter] = Field(default_factory=list, max_length=20)
    sort: CrmSort | None = None
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("object_type")
    @classmethod
    def _validate_object_type(cls, v: str) -> str:
        if v not in CRM_OBJECT_TYPES:
            raise ValueError(f"object_type must be one of: {', '.join(CRM_OBJECT_TYPES)}")
        return v


def _build_filter_sql(filt: CrmFilter, idx: int, params: dict[str, Any]) -> str:
    field_pname = f"crmf_{idx}"
    val_pname = f"crmv_{idx}"
    params[field_pname] = filt.field

    if filt.op == "is_null":
        return f"(properties ->> :{field_pname}) IS NULL"

    if filt.op == "in":
        params[val_pname] = [str(v) for v in filt.value]
        return f"(properties ->> :{field_pname}) = ANY(:{val_pname})"

    if filt.op == "contains":
        params[val_pname] = f"%{filt.value}%"
        return f"(properties ->> :{field_pname}) ILIKE :{val_pname}"

    if filt.op in _NUMERIC_OPS:
        params[val_pname] = filt.value
        params[f"{val_pname}_re"] = _NUMERIC_RE
        sql_op = _OPERATORS[filt.op]
        # Guard non-numeric stored values with a regex check instead of an
        # unconditional ::numeric cast — a bad cast would raise a DB error
        # for the whole query; this way such rows just never match.
        return (
            f"(CASE WHEN properties ->> :{field_pname} ~ :{val_pname}_re "
            f"THEN (properties ->> :{field_pname})::numeric END) "
            f"{sql_op} :{val_pname}"
        )

    # eq / ne — plain text comparison
    params[val_pname] = str(filt.value)
    sql_op = _OPERATORS[filt.op]
    return f"(properties ->> :{field_pname}) {sql_op} :{val_pname}"


async def run_crm_query(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    principal_ids: list[str] | None,
    query: CrmQuery,
) -> list[dict[str, Any]]:
    """Execute a validated CrmQuery against crm_objects, ACL-filtered by
    the caller's resolved principal set (None = legacy/unfiltered, same
    convention as acl_predicate_sql)."""
    params: dict[str, Any] = {
        "tid": tenant_id, "otype": query.object_type, "lim": query.limit,
    }
    where = ["co.tenant_id = :tid", "co.object_type = :otype"]
    for i, filt in enumerate(query.filters):
        where.append(_build_filter_sql(filt, i, params))

    acl_filter = ""
    if principal_ids is not None:
        params["principal_ids"] = principal_ids
        acl_filter = crm_acl_predicate_sql(
            crm_alias="co", source_alias="s", tenant_id_param="tid",
        )

    order_sql = "co.created_at DESC"
    if query.sort:
        params["sort_field"] = query.sort.field
        direction = "ASC" if query.sort.direction == "asc" else "DESC"
        order_sql = f"(properties ->> :sort_field) {direction} NULLS LAST"

    sql = text(
        "SELECT co.id, co.object_type, co.external_id, co.owner_principal_id, "
        "co.properties, co.created_at, co.updated_at, co.document_id "
        "FROM crm_objects co "
        "JOIN sources s ON s.id = co.source_id "
        f"WHERE {' AND '.join(where)}"
        f"{acl_filter} "
        f"ORDER BY {order_sql} "
        "LIMIT :lim",
    )
    result = await session.execute(sql, params)
    return [
        {
            "id": str(r.id),
            "object_type": r.object_type,
            "external_id": r.external_id,
            "owner_principal_id": r.owner_principal_id,
            "properties": r.properties,
            "created_at": str(r.created_at),
            "updated_at": str(r.updated_at),
            "document_id": str(r.document_id) if r.document_id else None,
        }
        for r in result.fetchall()
    ]
