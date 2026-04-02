# raasoa-client

Python client SDK and CLI for the RAASOA Enterprise RAG service.

## Installation

```bash
pip install raasoa-client
# With CLI support:
pip install raasoa-client[cli]
```

## Usage

```python
from raasoa_client import RAGClient

client = RAGClient("http://localhost:8000")
doc = client.ingest("document.pdf")
results = client.search("How does auth work?")
```

## CLI

```bash
raasoa ingest document.pdf
raasoa search "query"
raasoa documents
raasoa health
```
