"""Pluggable document-type schema validation.

Different document types have different quality criteria:
- A PDF needs good parse quality
- A SKILL.md needs valid YAML, mandatory sections, version bump
- A Policy needs effective date, approver, version

Schema checks run as part of the quality gate and contribute
to the quality score. They're configurable per document type.

Built-in schemas:
- "skill": YAML frontmatter required, mandatory sections, version tracking
- "policy": effective date, approver, version required
- "playbook": phases, deliverables sections required

Custom schemas can be registered per tenant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SchemaFinding:
    """A single schema validation finding."""

    check: str
    severity: str  # "critical", "warning", "info"
    message: str


@dataclass
class SchemaResult:
    """Result of schema validation for a document."""

    doc_type: str
    valid: bool
    score_penalty: float  # 0.0 = no penalty, 1.0 = max penalty
    findings: list[SchemaFinding] = field(default_factory=list)


# ── Built-in Schema Definitions ──────────────────────────

SKILL_REQUIRED_FRONTMATTER = ["name", "description"]
SKILL_RECOMMENDED_FRONTMATTER = ["version", "owner", "executor", "ampel"]
SKILL_REQUIRED_SECTIONS = ["zweck", "sop", "dod"]

POLICY_REQUIRED_FRONTMATTER = ["version"]
POLICY_RECOMMENDED_FRONTMATTER = ["effective_date", "approved_by"]
POLICY_REQUIRED_SECTIONS = ["scope", "policy"]

PLAYBOOK_REQUIRED_SECTIONS = ["phase", "deliverable"]


def check_skill_schema(
    frontmatter: dict[str, Any],
    content: str,
    sections: list[str],
) -> SchemaResult:
    """Validate a SKILL.md document."""
    findings: list[SchemaFinding] = []
    penalty = 0.0

    # Check required frontmatter
    for key in SKILL_REQUIRED_FRONTMATTER:
        if key not in frontmatter:
            findings.append(SchemaFinding(
                check="missing_frontmatter",
                severity="critical",
                message=f"Required frontmatter field '{key}' is missing",
            ))
            penalty += 0.15

    # Check recommended frontmatter
    for key in SKILL_RECOMMENDED_FRONTMATTER:
        if key not in frontmatter:
            findings.append(SchemaFinding(
                check="missing_recommended_field",
                severity="warning",
                message=f"Recommended field '{key}' not in frontmatter",
            ))
            penalty += 0.05

    # Check required sections (case-insensitive heading search)
    content_lower = content.lower()
    for section in SKILL_REQUIRED_SECTIONS:
        if section not in content_lower:
            findings.append(SchemaFinding(
                check="missing_section",
                severity="warning",
                message=f"Required section '{section}' not found in content",
            ))
            penalty += 0.1

    # Check version is present and looks valid
    version = frontmatter.get("version")
    if version and not re.match(r"\d+\.\d+", str(version)):
        findings.append(SchemaFinding(
            check="invalid_version",
            severity="warning",
            message=f"Version '{version}' should be semver (e.g. 1.0)",
        ))
        penalty += 0.05

    return SchemaResult(
        doc_type="skill",
        valid=penalty < 0.3,
        score_penalty=min(penalty, 0.5),
        findings=findings,
    )


def check_policy_schema(
    frontmatter: dict[str, Any],
    content: str,
    sections: list[str],
) -> SchemaResult:
    """Validate a policy document."""
    findings: list[SchemaFinding] = []
    penalty = 0.0

    for key in POLICY_REQUIRED_FRONTMATTER:
        if key not in frontmatter:
            findings.append(SchemaFinding(
                check="missing_frontmatter",
                severity="warning",
                message=f"Policy should have '{key}' in frontmatter",
            ))
            penalty += 0.1

    for key in POLICY_RECOMMENDED_FRONTMATTER:
        if key not in frontmatter:
            findings.append(SchemaFinding(
                check="missing_recommended_field",
                severity="info",
                message=f"Consider adding '{key}' to policy frontmatter",
            ))
            penalty += 0.03

    return SchemaResult(
        doc_type="policy",
        valid=True,
        score_penalty=min(penalty, 0.3),
        findings=findings,
    )


# ── Schema Registry ──────────────────────────────────────

SCHEMA_REGISTRY: dict[str, Any] = {
    "skill": check_skill_schema,
    "policy": check_policy_schema,
}


def run_schema_check(
    doc_type_hint: str | None,
    frontmatter: dict[str, Any],
    content: str,
    sections: list[str] | None = None,
) -> SchemaResult | None:
    """Run schema validation if a matching schema exists.

    doc_type_hint can come from:
    - frontmatter['type'] or frontmatter['doc_type']
    - filename suffix (e.g. SKILL.md → "skill")
    - explicit doc_type parameter

    Returns None if no schema matches.
    """
    # Determine document type
    dtype = (
        doc_type_hint
        or frontmatter.get("type")
        or frontmatter.get("doc_type")
        or frontmatter.get("schema")
    )

    if not dtype:
        return None

    dtype = dtype.lower().strip()
    check_fn = SCHEMA_REGISTRY.get(dtype)
    if not check_fn:
        return None

    return check_fn(frontmatter, content, sections or [])  # type: ignore[no-any-return]
