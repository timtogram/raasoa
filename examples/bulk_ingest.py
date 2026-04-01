"""
Example: Bulk ingest all documents from a directory.

Usage:
    python bulk_ingest.py /path/to/documents
"""

import sys
from pathlib import Path

from raasoa_client import RAGClient

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json", ".xml", ".html"}


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python bulk_ingest.py <directory>")
        sys.exit(1)

    directory = Path(sys.argv[1])
    if not directory.is_dir():
        print(f"Not a directory: {directory}")
        sys.exit(1)

    client = RAGClient("http://localhost:8000")

    # Check health first
    health = client.health()
    if health.get("status") != "healthy":
        print(f"Service unhealthy: {health}")
        sys.exit(1)

    # Find all supported files
    files = [
        f for f in directory.rglob("*")
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    print(f"Found {len(files)} documents to ingest\n")

    success = 0
    failed = 0
    for f in files:
        try:
            result = client.ingest(f)
            print(f"  [OK] {f.name} -> {result.chunk_count} chunks")
            success += 1
        except Exception as e:
            print(f"  [FAIL] {f.name} -> {e}")
            failed += 1

    print(f"\nDone: {success} succeeded, {failed} failed")


if __name__ == "__main__":
    main()
