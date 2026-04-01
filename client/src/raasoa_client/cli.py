from __future__ import annotations

import sys
from pathlib import Path

try:
    import click
    from rich.console import Console
    from rich.table import Table
except ImportError:
    print("CLI dependencies missing. Install with: pip install raasoa-client[cli]")
    sys.exit(1)

from raasoa_client.client import RAGClient

console = Console()


@click.group()
@click.option("--url", default="http://localhost:8000", help="RAASOA API URL")
@click.option("--tenant", default="00000000-0000-0000-0000-000000000001", help="Tenant ID")
@click.pass_context
def main(ctx: click.Context, url: str, tenant: str) -> None:
    """RAASOA — Enterprise RAG as a Service CLI"""
    ctx.ensure_object(dict)
    ctx.obj["client"] = RAGClient(base_url=url, tenant_id=tenant)


@main.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.pass_context
def ingest(ctx: click.Context, file_path: str) -> None:
    """Ingest a document (PDF, DOCX, TXT, MD)."""
    client: RAGClient = ctx.obj["client"]
    with console.status(f"Ingesting {Path(file_path).name}..."):
        result = client.ingest(file_path)

    console.print(f"[green]Ingested:[/green] {result.title}")
    console.print(f"  Chunks: {result.chunk_count}")
    console.print(f"  Version: {result.version}")
    console.print(f"  Model: {result.embedding_model}")
    console.print(f"  ID: {result.document_id}")


@main.command()
@click.argument("query")
@click.option("--top-k", default=5, help="Number of results")
@click.pass_context
def search(ctx: click.Context, query: str, top_k: int) -> None:
    """Search for relevant content."""
    client: RAGClient = ctx.obj["client"]
    with console.status("Searching..."):
        response = client.search(query, top_k=top_k)

    if not response.results:
        console.print("[yellow]No results found.[/yellow]")
        return

    console.print(f"[bold]Query:[/bold] {response.query}")
    console.print(
        f"[bold]Confidence:[/bold] {response.confidence.retrieval_confidence:.1%} "
        f"({'answerable' if response.confidence.answerable else 'uncertain'}) "
        f"from {response.confidence.source_count} sources\n"
    )

    for i, hit in enumerate(response.results, 1):
        console.print(f"[bold cyan]#{i}[/bold cyan] [dim]score={hit.score:.4f}[/dim]")
        if hit.section_title:
            console.print(f"  [dim]Section: {hit.section_title}[/dim]")
        # Show first 200 chars of text
        text = hit.text[:200] + "..." if len(hit.text) > 200 else hit.text
        console.print(f"  {text}\n")


@main.command(name="documents")
@click.option("--limit", default=20, help="Max documents to show")
@click.pass_context
def list_documents(ctx: click.Context, limit: int) -> None:
    """List ingested documents."""
    client: RAGClient = ctx.obj["client"]
    docs = client.documents(limit=limit)

    if not docs:
        console.print("[yellow]No documents found.[/yellow]")
        return

    table = Table(title="Documents")
    table.add_column("Title", style="cyan")
    table.add_column("Chunks", justify="right")
    table.add_column("Version", justify="right")
    table.add_column("Status")
    table.add_column("ID", style="dim")

    for doc in docs:
        table.add_row(
            doc.title or "(untitled)",
            str(doc.chunk_count),
            str(doc.version),
            doc.status,
            doc.id[:8] + "...",
        )

    console.print(table)


@main.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Check service health."""
    client: RAGClient = ctx.obj["client"]
    try:
        h = client.health()
        status = h.get("status", "unknown")
        if status == "healthy":
            console.print(f"[green]Status: {status}[/green]")
        else:
            console.print(f"[red]Status: {status}[/red]")
        console.print(f"  Database: {h.get('database', 'unknown')}")
        console.print(f"  pgvector: {h.get('pgvector', 'unknown')}")
    except Exception as e:
        console.print(f"[red]Connection failed:[/red] {e}")


if __name__ == "__main__":
    main()
