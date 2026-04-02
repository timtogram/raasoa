"""Source-level data contract validation.

Validates incoming documents against configurable rules before indexing.
Catches junk data early — before it gets embedded and pollutes search.

Rules:
- Minimum content length
- Required metadata fields per source
- Status filters (e.g. only "Published" Confluence pages)
- Content blocklist patterns (boilerplate, auto-generated)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ValidationResult:
    """Result of data contract validation."""

    valid: bool
    reason: str | None = None


# Source-specific required metadata keys
SOURCE_REQUIRED_METADATA: dict[str, list[str]] = {
    # Example: Confluence pages should have a status
    # "confluence": ["status"],
}

# Source-specific allowed status values
# If configured, documents with other statuses are rejected
SOURCE_STATUS_FILTERS: dict[str, set[str]] = {
    # Example: Only sync published Confluence pages
    # "confluence": {"current", "published"},
}

# Minimum content length per source (bytes)
SOURCE_MIN_CONTENT_LENGTH: dict[str, int] = {
    "default": 50,
}

# Content patterns that indicate junk (auto-generated, templates)
BLOCKLIST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^This page is auto-generated", re.IGNORECASE),
    re.compile(r"^\s*\[?TODO\]?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Lorem ipsum", re.IGNORECASE),
]


def validate_webhook_payload(
    source: str,
    content: str | None,
    metadata: dict,
    title: str | None = None,
) -> ValidationResult:
    """Validate an incoming webhook payload against data contracts.

    Called before ingestion to catch bad data early.
    """
    # 1. Content must exist for create/update
    if not content or not content.strip():
        return ValidationResult(False, "Empty content")

    # 2. Minimum content length
    min_len = SOURCE_MIN_CONTENT_LENGTH.get(
        source, SOURCE_MIN_CONTENT_LENGTH["default"]
    )
    if len(content.strip()) < min_len:
        return ValidationResult(
            False,
            f"Content too short ({len(content.strip())} < {min_len} chars)",
        )

    # 3. Required metadata fields
    required = SOURCE_REQUIRED_METADATA.get(source, [])
    missing = [k for k in required if k not in metadata]
    if missing:
        return ValidationResult(
            False, f"Missing required metadata: {', '.join(missing)}"
        )

    # 4. Status filter
    allowed_statuses = SOURCE_STATUS_FILTERS.get(source)
    if allowed_statuses:
        doc_status = metadata.get("status", "").lower()
        if doc_status and doc_status not in allowed_statuses:
            return ValidationResult(
                False,
                f"Status '{doc_status}' not in allowed: {allowed_statuses}",
            )

    # 5. Blocklist patterns
    for pattern in BLOCKLIST_PATTERNS:
        if pattern.search(content):
            return ValidationResult(
                False,
                f"Content matches blocklist pattern: {pattern.pattern[:40]}",
            )

    return ValidationResult(True)
