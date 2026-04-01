"""
RAASOA Quickstart — Ingest a document and search in 10 lines.

Prerequisites:
    docker compose up -d
    pip install raasoa-client
"""

from raasoa_client import RAGClient

client = RAGClient("http://localhost:8000")

# Check service health
print("Service:", client.health())

# Ingest a document
doc = client.ingest("../README.md")
print(f"\nIngested: {doc.title} ({doc.chunk_count} chunks)")

# Search
response = client.search("How does hybrid search work?")
print(f"\nSearch results (confidence: {response.confidence.retrieval_confidence:.0%}):\n")
for i, hit in enumerate(response.results, 1):
    print(f"  #{i} [score={hit.score:.4f}] {hit.text[:120]}...")
