from dataclasses import dataclass
from typing import Any

import tiktoken

from raasoa.config import settings


@dataclass
class ChunkResult:
    text: str
    index: int
    section_title: str | None = None
    chunk_type: str = "text"
    token_count: int = 0
    page_number: int | None = None
    source_location: str | None = None  # "Page 5", "Slide 3", "Sheet: Revenue"


_encoder: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


def count_tokens(text: str) -> int:
    return len(_get_encoder().encode(text))


def recursive_split(
    text: str,
    chunk_size: int = settings.chunk_size,
    chunk_overlap: int = settings.chunk_overlap,
    separators: list[str] | None = None,
) -> list[str]:
    """Split text recursively by trying separators in order of preference."""
    if separators is None:
        separators = ["\n\n", "\n", ". ", " ", ""]

    tokens = count_tokens(text)
    if tokens <= chunk_size:
        return [text] if text.strip() else []

    # Find the best separator that actually splits the text
    chosen_sep = ""
    for sep in separators:
        if sep == "":
            chosen_sep = sep
            break
        if sep in text:
            chosen_sep = sep
            break

    # Split by chosen separator
    if chosen_sep:
        parts = text.split(chosen_sep)
    else:
        # Character-level split as last resort
        enc = _get_encoder()
        encoded = enc.encode(text)
        parts = [enc.decode(encoded[i:i + chunk_size]) for i in range(0, len(encoded), chunk_size)]
        return parts

    # Merge parts into chunks respecting token limits
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_tokens = 0

    for part in parts:
        part_tokens = count_tokens(part)
        if part_tokens == 0:
            continue

        if current_tokens + part_tokens > chunk_size and current_chunk:
            chunks.append(chosen_sep.join(current_chunk))

            # Overlap: keep last parts that fit within overlap budget
            overlap_parts: list[str] = []
            overlap_tokens = 0
            for prev_part in reversed(current_chunk):
                pt = count_tokens(prev_part)
                if overlap_tokens + pt > chunk_overlap:
                    break
                overlap_parts.insert(0, prev_part)
                overlap_tokens += pt

            current_chunk = overlap_parts
            current_tokens = overlap_tokens

        # If a single part exceeds chunk_size, recursively split it
        if part_tokens > chunk_size:
            if current_chunk:
                chunks.append(chosen_sep.join(current_chunk))
                current_chunk = []
                current_tokens = 0
            if chosen_sep in separators:
                remaining_seps = separators[separators.index(chosen_sep) + 1:]
            else:
                remaining_seps = separators[1:]
            sub_chunks = recursive_split(part, chunk_size, chunk_overlap, remaining_seps)
            chunks.extend(sub_chunks)
        else:
            current_chunk.append(part)
            current_tokens += part_tokens

    if current_chunk:
        final = chosen_sep.join(current_chunk)
        if final.strip():
            chunks.append(final)

    return chunks


def chunk_document(
    full_text: str,
    title: str | None = None,
    sections: Any = None,
    chunk_size: int = settings.chunk_size,
    chunk_overlap: int = settings.chunk_overlap,
) -> list[ChunkResult]:
    """Chunk a document into smaller pieces with token counting.

    If sections with page_number/source_location are provided,
    chunks inherit the location from their source section.
    """
    # Build a character-offset → section map for location tracking
    section_map: list[tuple[int, int, str | None, int | None]] = []
    if sections:
        offset = 0
        for s in sections:
            # Find where this section's text appears in full_text
            idx = full_text.find(s.text, offset) if hasattr(s, "text") else -1
            if idx >= 0:
                loc = getattr(s, "source_location", None)
                pg = getattr(s, "page_number", None)
                section_map.append((idx, idx + len(s.text), loc, pg))
                offset = idx + 1

    raw_chunks = recursive_split(full_text, chunk_size, chunk_overlap)

    results: list[ChunkResult] = []
    char_offset = 0
    for i, chunk_text in enumerate(raw_chunks):
        chunk_text = chunk_text.strip()
        if not chunk_text:
            continue

        # Find which section this chunk belongs to
        chunk_start = full_text.find(chunk_text, char_offset)
        if chunk_start >= 0:
            char_offset = chunk_start + 1

        source_location: str | None = None
        page_number: int | None = None
        if chunk_start >= 0:
            for sec_start, sec_end, loc, pg in section_map:
                if sec_start <= chunk_start < sec_end:
                    source_location = loc
                    page_number = pg
                    break

        results.append(
            ChunkResult(
                text=chunk_text,
                index=i,
                section_title=title,
                token_count=count_tokens(chunk_text),
                page_number=page_number,
                source_location=source_location,
            )
        )

    return results
