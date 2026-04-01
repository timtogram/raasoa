from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IngestResult:
    document_id: str
    title: str | None
    status: str
    chunk_count: int
    version: int
    embedding_model: str | None
    message: str

    @classmethod
    def from_dict(cls, data: dict) -> IngestResult:
        return cls(
            document_id=data["document_id"],
            title=data.get("title"),
            status=data["status"],
            chunk_count=data["chunk_count"],
            version=data["version"],
            embedding_model=data.get("embedding_model"),
            message=data["message"],
        )


@dataclass
class ChunkHit:
    chunk_id: str
    document_id: str
    text: str
    section_title: str | None
    chunk_type: str
    score: float
    semantic_rank: int | None = None
    lexical_rank: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> ChunkHit:
        return cls(
            chunk_id=data["chunk_id"],
            document_id=data["document_id"],
            text=data["text"],
            section_title=data.get("section_title"),
            chunk_type=data["chunk_type"],
            score=data["score"],
            semantic_rank=data.get("semantic_rank"),
            lexical_rank=data.get("lexical_rank"),
        )


@dataclass
class ConfidenceInfo:
    retrieval_confidence: float
    source_count: int
    top_score: float
    answerable: bool

    @classmethod
    def from_dict(cls, data: dict) -> ConfidenceInfo:
        return cls(
            retrieval_confidence=data["retrieval_confidence"],
            source_count=data["source_count"],
            top_score=data["top_score"],
            answerable=data["answerable"],
        )


@dataclass
class SearchResponse:
    query: str
    results: list[ChunkHit]
    confidence: ConfidenceInfo

    @classmethod
    def from_dict(cls, data: dict) -> SearchResponse:
        return cls(
            query=data["query"],
            results=[ChunkHit.from_dict(r) for r in data["results"]],
            confidence=ConfidenceInfo.from_dict(data["confidence"]),
        )


@dataclass
class DocumentInfo:
    id: str
    title: str | None
    status: str
    chunk_count: int
    version: int

    @classmethod
    def from_dict(cls, data: dict) -> DocumentInfo:
        return cls(
            id=data["id"],
            title=data.get("title"),
            status=data["status"],
            chunk_count=data["chunk_count"],
            version=data["version"],
        )
