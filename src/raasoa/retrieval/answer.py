"""Grounded answer synthesis with citations — and honest refusal.

The differentiator: when retrieval confidence is too low (or no sources
clear the bar), RAASOA *refuses to answer* instead of hallucinating.
When it does answer, every claim is grounded in the retrieved chunks and
cited as ``[n]``.

The LLM call is isolated behind ``synthesize_answer``; the prompt builder
and citation extraction are pure functions so they can be unit-tested
without a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from raasoa.config import settings

REFUSAL_TEXT = (
    "I don't have a reliable answer to that in the knowledge base. "
    "The available sources are too weak or unrelated to answer confidently."
)

ANSWER_SYSTEM = (
    "You answer strictly from the SOURCES the user provides. "
    "Do not think out loud or explain your reasoning. "
    "Output ONLY the final answer text. Rules: "
    "(1) Use only facts found in the sources — never outside knowledge. "
    "(2) Cite every fact with [n] referencing the source number. "
    "(3) If the sources do not contain the answer, output exactly the single "
    "word INSUFFICIENT and nothing else. "
    "(4) Be concise and answer in the language of the question."
)

ANSWER_USER = """QUESTION:
{query}

SOURCES:
{sources}"""

# Kept for backward-compat / direct prompt construction in tests.
ANSWER_PROMPT = ANSWER_SYSTEM + "\n\n" + ANSWER_USER


@dataclass
class SourceChunk:
    """One retrieved chunk offered to the synthesizer."""

    n: int
    chunk_id: str
    document_id: str
    document_title: str | None
    source_url: str | None
    source_location: str | None
    text: str


def build_sources_block(chunks: list[SourceChunk]) -> str:
    """Render the numbered SOURCES block for the prompt."""
    parts = []
    for c in chunks:
        header = f"[{c.n}]"
        if c.document_title:
            header += f" {c.document_title}"
        if c.source_location:
            header += f" ({c.source_location})"
        parts.append(f"{header}\n{c.text.strip()}")
    return "\n\n".join(parts)


def build_answer_prompt(query: str, chunks: list[SourceChunk]) -> str:
    return ANSWER_PROMPT.format(query=query, sources=build_sources_block(chunks))


def build_user_message(query: str, chunks: list[SourceChunk]) -> str:
    return ANSWER_USER.format(query=query, sources=build_sources_block(chunks))


def clean_model_output(raw: str) -> str:
    """Strip qwen-style thinking blocks and surrounding whitespace."""
    txt = (raw or "").strip()
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.DOTALL).strip()
    # Drop an unclosed leading think block (ran out of budget mid-thought).
    if "<think>" in txt and "</think>" not in txt:
        txt = ""
    return txt.strip()


def is_insufficient(answer: str) -> bool:
    """True if the model signalled it could not answer from the sources."""
    a = answer.strip().upper()
    return a == "" or a.startswith("INSUFFICIENT")


def cited_source_numbers(answer: str) -> set[int]:
    """Extract the [n] citation markers actually used in the answer."""
    return {int(m) for m in re.findall(r"\[(\d+)\]", answer)}


def valid_citation_numbers(answer: str, n_sources: int) -> set[int]:
    """Citation markers that point at a real source (1..n_sources)."""
    return {n for n in cited_source_numbers(answer) if 1 <= n <= n_sources}


def is_grounded(answer: str, n_sources: int) -> bool:
    """A grounded answer cites at least one real source.

    Used as a model-agnostic backstop: an answer with no valid citation is
    either leaked reasoning or an ungrounded claim, so the endpoint refuses.
    """
    return bool(valid_citation_numbers(answer, n_sources))


async def synthesize_answer(
    query: str,
    chunks: list[SourceChunk],
    *,
    base_url: str | None = None,
    model: str | None = None,
) -> str:
    """Call the chat LLM to synthesize a grounded answer.

    Returns the raw (cleaned) answer text, or "INSUFFICIENT". Network/parse
    failures degrade to "INSUFFICIENT" so the endpoint refuses rather than
    erroring.
    """
    base_url = base_url or settings.ollama_base_url
    model = model or settings.ollama_chat_model
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": ANSWER_SYSTEM},
                        {"role": "user",
                         "content": build_user_message(query, chunks)},
                    ],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.0, "num_predict": 1024},
                },
            )
            resp.raise_for_status()
            content = resp.json().get("message", {}).get("content", "")
            return clean_model_output(content)
    except (httpx.HTTPError, ValueError):
        return "INSUFFICIENT"
