"""Regression guard: every ORM model's __tablename__ must match the table
it was written against in the alembic migrations.

These 11 tables (retrieval_feedback, knowledge_syntheses, knowledge_index,
audit_events, job_queue, api_keys, usage_events, principal_groups,
principal_memberships, source_acl_grants, crm_objects) existed only as
raw migrations with no declarative model for a long time, which meant
`alembic revision --autogenerate` (which diffs against Base.metadata)
would have emitted a DROP TABLE for every one of them. This test doesn't
re-verify full column parity (that's alembic autogenerate's job, run
manually — see the migration this test ships alongside), it just makes
sure nobody silently renames/removes a model's __tablename__ (or the
class itself) without noticing the table it's supposed to represent.
"""
from __future__ import annotations

from raasoa.models.acl import AclEntry
from raasoa.models.chunk import Chunk
from raasoa.models.claim import Claim
from raasoa.models.crm import CrmObject
from raasoa.models.document import Document, DocumentVersion
from raasoa.models.feedback import (
    KnowledgeIndexEntry,
    KnowledgeSynthesis,
    RetrievalFeedback,
)
from raasoa.models.governance import (
    ChangeEvent,
    ConflictCandidate,
    CorrectionRecord,
    IngestionRun,
    QualityFinding,
    RetrievalLog,
    ReviewTask,
)
from raasoa.models.ops import AuditEvent, QueuedJob
from raasoa.models.principals import PrincipalGroup, PrincipalMembership, SourceAclGrant
from raasoa.models.saas import ApiKey, UsageEvent
from raasoa.models.source import Source, SyncCursor
from raasoa.models.tenant import Tenant

# (model class, expected __tablename__) — every table that once existed
# only in migrations (never as a model), plus a handful of pre-existing
# models thrown in for good measure.
EXPECTED_TABLENAMES = [
    (RetrievalFeedback, "retrieval_feedback"),
    (KnowledgeSynthesis, "knowledge_syntheses"),
    (KnowledgeIndexEntry, "knowledge_index"),
    (AuditEvent, "audit_events"),
    (QueuedJob, "job_queue"),
    (ApiKey, "api_keys"),
    (UsageEvent, "usage_events"),
    (PrincipalGroup, "principal_groups"),
    (PrincipalMembership, "principal_memberships"),
    (SourceAclGrant, "source_acl_grants"),
    (CrmObject, "crm_objects"),
    # Pre-existing models, included as a control group.
    (AclEntry, "acl_entries"),
    (Chunk, "chunks"),
    (Claim, "claims"),
    (Document, "documents"),
    (DocumentVersion, "document_versions"),
    (ChangeEvent, "change_events"),
    (ConflictCandidate, "conflict_candidates"),
    (CorrectionRecord, "corrections"),
    (IngestionRun, "ingestion_runs"),
    (QualityFinding, "quality_findings"),
    (RetrievalLog, "retrieval_logs"),
    (ReviewTask, "review_tasks"),
    (Source, "sources"),
    (SyncCursor, "sync_cursors"),
    (Tenant, "tenants"),
]


def test_all_expected_models_exist_with_correct_tablename() -> None:
    mismatches = [
        f"{model.__name__}.__tablename__ == {model.__tablename__!r}, expected {expected!r}"
        for model, expected in EXPECTED_TABLENAMES
        if model.__tablename__ != expected
    ]
    assert not mismatches, "\n".join(mismatches)


def test_no_duplicate_tablenames_among_expected_models() -> None:
    names = [expected for _, expected in EXPECTED_TABLENAMES]
    assert len(names) == len(set(names))


def test_previously_migration_only_tables_now_have_models() -> None:
    """The specific regression this file guards against: these 11 tables
    used to exist only in alembic migrations, with no SQLAlchemy model —
    meaning `alembic revision --autogenerate` would have proposed
    DROP TABLE for every single one of them."""
    previously_missing = {
        "retrieval_feedback": RetrievalFeedback,
        "knowledge_syntheses": KnowledgeSynthesis,
        "knowledge_index": KnowledgeIndexEntry,
        "audit_events": AuditEvent,
        "job_queue": QueuedJob,
        "api_keys": ApiKey,
        "usage_events": UsageEvent,
        "principal_groups": PrincipalGroup,
        "principal_memberships": PrincipalMembership,
        "source_acl_grants": SourceAclGrant,
        "crm_objects": CrmObject,
    }
    assert len(previously_missing) == 11
    for table_name, model in previously_missing.items():
        assert model.__tablename__ == table_name


def test_tenant_and_source_have_previously_missing_columns() -> None:
    """tenants.admin_api_enabled and sources.default_visibility were added
    by alembic m3f4a5b6c7d8 but were missing from the ORM models until
    this fix — guard against that regressing."""
    assert "admin_api_enabled" in Tenant.__table__.columns
    assert "default_visibility" in Source.__table__.columns
