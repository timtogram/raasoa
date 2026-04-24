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


# ---------- Ingest ----------

@main.command()
@click.argument("file_path", type=click.Path(exists=True))
@click.pass_context
def ingest(ctx: click.Context, file_path: str) -> None:
    """Ingest a document (PDF, DOCX, TXT, MD)."""
    client: RAGClient = ctx.obj["client"]
    with console.status(f"Ingesting {Path(file_path).name}..."):
        result = client.ingest(file_path)

    console.print(f"[green]✓ Ingested:[/green] {result.title}")
    console.print(f"  Chunks: {result.chunk_count}  |  Version: {result.version}")
    console.print(f"  Model: {result.embedding_model}")
    console.print(f"  Quality: {result.quality_score or 'N/A'}  |  Status: {result.review_status}")
    console.print(f"  ID: {result.document_id}")


# ---------- Search ----------

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
        f"[bold]Routed to:[/bold] {getattr(response, 'routed_to', 'rag')}"
    )
    console.print(
        f"[bold]Confidence:[/bold] {response.confidence.retrieval_confidence:.1%} "
        f"({'answerable' if response.confidence.answerable else 'uncertain'}) "
        f"from {response.confidence.source_count} sources\n"
    )

    # Show structured answer if available
    structured = getattr(response, "structured", None)
    if structured:
        console.print(f"[bold green]Answer:[/bold green] {structured.get('answer', '')}\n")

    for i, hit in enumerate(response.results, 1):
        console.print(f"[bold cyan]#{i}[/bold cyan] [dim]score={hit.score:.4f}[/dim]")
        if hit.section_title:
            console.print(f"  [dim]Section: {hit.section_title}[/dim]")
        text = hit.text[:200] + "..." if len(hit.text) > 200 else hit.text
        console.print(f"  {text}\n")


# ---------- Documents ----------

@main.command(name="documents")
@click.option("--limit", default=20, help="Max documents to show")
@click.pass_context
def list_documents(ctx: click.Context, limit: int) -> None:
    """List ingested documents."""
    client: RAGClient = ctx.obj["client"]
    result = client.documents(limit=limit)
    docs = result.get("items", []) if isinstance(result, dict) else result

    if not docs:
        console.print("[yellow]No documents found.[/yellow]")
        return

    table = Table(title="Documents")
    table.add_column("Title", style="cyan")
    table.add_column("Chunks", justify="right")
    table.add_column("Quality", justify="right")
    table.add_column("Tier")
    table.add_column("Status")
    table.add_column("ID", style="dim")

    for doc in docs:
        quality = f"{doc.quality_score:.2f}" if doc.quality_score else "—"
        table.add_row(
            doc.title or "(untitled)",
            str(doc.chunk_count),
            quality,
            getattr(doc, "index_tier", "hot"),
            doc.status,
            doc.id[:8] + "...",
        )

    console.print(table)

    if isinstance(result, dict) and result.get("has_more"):
        console.print(f"[dim]More results available. Use --cursor {result['next_cursor']}[/dim]")


@main.command(name="delete")
@click.argument("document_id")
@click.pass_context
def delete_document(ctx: click.Context, document_id: str) -> None:
    """Soft-delete a document."""
    client: RAGClient = ctx.obj["client"]
    client.delete_document(document_id)
    console.print(f"[green]✓ Deleted document {document_id}[/green]")


# ---------- Quality ----------

@main.command(name="quality")
@click.argument("document_id")
@click.pass_context
def quality_report(ctx: click.Context, document_id: str) -> None:
    """Show quality report for a document."""
    client: RAGClient = ctx.obj["client"]
    report = client.quality_report(document_id)

    console.print(f"[bold]{report.get('title', 'Unknown')}[/bold]")
    console.print(f"  Quality Score: {report.get('quality_score', 'N/A')}")
    console.print(f"  Review Status: {report.get('review_status', 'N/A')}")
    console.print(f"  Conflict Status: {report.get('conflict_status', 'N/A')}")

    findings = report.get("findings", [])
    if findings:
        console.print(f"\n[bold]Findings ({len(findings)}):[/bold]")
        for f in findings:
            icon = "🔴" if f["severity"] == "error" else "🟡" if f["severity"] == "warning" else "ℹ️"
            console.print(f"  {icon} [{f['severity']}] {f['finding_type']}")


@main.command(name="findings")
@click.option("--severity", help="Filter by severity (error, warning, info)")
@click.option("--limit", default=20)
@click.pass_context
def list_findings(ctx: click.Context, severity: str | None, limit: int) -> None:
    """List quality findings across all documents."""
    client: RAGClient = ctx.obj["client"]
    findings = client.quality_findings(severity=severity, limit=limit)

    if not findings:
        console.print("[green]No quality findings.[/green]")
        return

    table = Table(title="Quality Findings")
    table.add_column("Type")
    table.add_column("Severity")
    table.add_column("Document", style="dim")
    for f in findings:
        table.add_row(f["finding_type"], f["severity"], str(f["document_id"])[:8] + "...")
    console.print(table)


# ---------- Conflicts ----------

@main.command(name="conflicts")
@click.option("--status", help="Filter by status (new, resolved)")
@click.option("--limit", default=20)
@click.pass_context
def list_conflicts(ctx: click.Context, status: str | None, limit: int) -> None:
    """List conflict candidates."""
    client: RAGClient = ctx.obj["client"]
    conflicts = client.conflicts(status=status, limit=limit)

    if not conflicts:
        console.print("[green]No conflicts found.[/green]")
        return

    table = Table(title="Conflicts")
    table.add_column("Type")
    table.add_column("Confidence", justify="right")
    table.add_column("Status")
    table.add_column("Doc A", style="dim")
    table.add_column("Doc B", style="dim")
    table.add_column("ID", style="dim")

    for c in conflicts:
        conf = f"{c['confidence']:.2f}" if c.get("confidence") else "—"
        table.add_row(
            c["conflict_type"],
            conf,
            c["status"],
            str(c["document_a_id"])[:8] + "...",
            str(c["document_b_id"])[:8] + "...",
            str(c["id"])[:8] + "...",
        )
    console.print(table)


@main.command(name="resolve")
@click.argument("conflict_id")
@click.argument("resolution", type=click.Choice(["keep_a", "keep_b", "keep_both", "reject_both"]))
@click.option("--comment", default="", help="Resolution comment")
@click.pass_context
def resolve_conflict(ctx: click.Context, conflict_id: str, resolution: str, comment: str) -> None:
    """Resolve a conflict."""
    client: RAGClient = ctx.obj["client"]
    result = client.resolve_conflict(conflict_id, resolution, comment)
    console.print(f"[green]✓ Conflict resolved: {resolution}[/green]")
    if result.get("superseded_document"):
        console.print(f"  Superseded: {result['superseded_document']}")


# ---------- Reviews ----------

@main.command(name="reviews")
@click.option("--status", help="Filter by status (new, approved, rejected)")
@click.option("--limit", default=20)
@click.pass_context
def list_reviews(ctx: click.Context, status: str | None, limit: int) -> None:
    """List review tasks."""
    client: RAGClient = ctx.obj["client"]
    reviews = client.reviews(status=status, limit=limit)

    if not reviews:
        console.print("[green]No review tasks.[/green]")
        return

    table = Table(title="Review Tasks")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Document", style="dim")
    table.add_column("ID", style="dim")

    for r in reviews:
        doc_id = str(r.get("document_id", ""))[:8] + "..." if r.get("document_id") else "—"
        table.add_row(r["task_type"], r["status"], doc_id, str(r["id"])[:8] + "...")
    console.print(table)


@main.command(name="approve")
@click.argument("review_id")
@click.option("--comment", default="")
@click.pass_context
def approve_review(ctx: click.Context, review_id: str, comment: str) -> None:
    """Approve a review task."""
    client: RAGClient = ctx.obj["client"]
    client.approve_review(review_id, comment)
    console.print(f"[green]✓ Review {review_id} approved[/green]")


@main.command(name="reject")
@click.argument("review_id")
@click.option("--comment", default="")
@click.pass_context
def reject_review(ctx: click.Context, review_id: str, comment: str) -> None:
    """Reject a review task."""
    client: RAGClient = ctx.obj["client"]
    client.reject_review(review_id, comment)
    console.print(f"[red]✗ Review {review_id} rejected[/red]")


# ---------- Health ----------

@main.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Check service health."""
    client: RAGClient = ctx.obj["client"]
    try:
        h = client.health()
        status = h.get("status", "unknown")
        if status == "healthy":
            console.print(f"[green]✓ Status: {status}[/green]")
        else:
            console.print(f"[yellow]⚠ Status: {status}[/yellow]")
        console.print(f"  Database: {h.get('database', 'unknown')}")
        console.print(f"  pgvector: {h.get('pgvector', 'unknown')}")

        emb = h.get("embedding", {})
        if emb:
            console.print(
                f"  Embedding: {emb.get('provider', '?')} → {emb.get('detail', '?')}"
            )

        claim = h.get("claim_extraction", {})
        if claim:
            enabled = "enabled" if claim.get("enabled") else "disabled"
            console.print(f"  Claims: {enabled} → {claim.get('detail', '?')}")
    except Exception as e:
        console.print(f"[red]Connection failed:[/red] {e}")


if __name__ == "__main__":
    main()
