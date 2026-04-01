"""
Example: Integrate RAASOA into your own FastAPI application.

This shows how to use RAASOA as a backend for a chatbot or Q&A API.
"""

from fastapi import FastAPI
from pydantic import BaseModel
from raasoa_client import RAGClient

app = FastAPI(title="My App with RAG")
rag = RAGClient("http://localhost:8000")


class Question(BaseModel):
    text: str


class Answer(BaseModel):
    answer: str
    sources: list[dict]
    confidence: float
    answerable: bool


@app.post("/ask", response_model=Answer)
async def ask(question: Question) -> Answer:
    """Ask a question and get an answer with sources."""
    response = rag.search(question.text, top_k=5)

    # Build context from top results
    context_parts = [hit.text for hit in response.results]
    context = "\n\n---\n\n".join(context_parts)

    # In production, you'd send this context to an LLM here:
    # answer = await llm.generate(question=question.text, context=context)

    return Answer(
        answer=f"Based on {len(response.results)} sources: {context[:200]}...",
        sources=[
            {
                "chunk_id": hit.chunk_id,
                "document_id": hit.document_id,
                "text": hit.text[:200],
                "score": hit.score,
            }
            for hit in response.results
        ],
        confidence=response.confidence.retrieval_confidence,
        answerable=response.confidence.answerable,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
