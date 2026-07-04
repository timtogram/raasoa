from __future__ import annotations

import json as json_module
import sys
from pathlib import Path
from typing import Any

import httpx

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
@click.option("--url", envvar="RAASOA_URL", default="http://localhost:8000", help="RAASOA API URL")
@click.option("--tenant", default="00000000-0000-0000-0000-000000000001", help="Tenant ID")
@click.option(
    "--api-key", envvar="RAASOA_API_KEY", default=None,
    help="API key (required for admin/RBAC commands; overrides --tenant when set)",
)
@click.pass_context
def main(ctx: click.Context, url: str, tenant: str, api_key: str | None) -> None:
    """RAASOA — Enterprise RAG as a Service CLI"""
    ctx.ensure_object(dict)
    ctx.obj["client"] = RAGClient(base_url=url, tenant_id=tenant, api_key=api_key)


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


# ---------- Answer (cited, honest-refusal) ----------

@main.command()
@click.argument("query")
@click.option("--top-k", default=6, help="Sources to consider")
@click.option("--min-confidence", default=0.3, help="Refuse below this confidence")
@click.pass_context
def answer(ctx: click.Context, query: str, top_k: int, min_confidence: float) -> None:
    """Answer a question with inline citations, or refuse honestly."""
    client: RAGClient = ctx.obj["client"]
    with console.status("Thinking..."):
        result = client.answer(query, top_k=top_k, min_confidence=min_confidence)

    if not result.get("answered"):
        reason = result.get("refusal_reason") or "insufficient sources"
        console.print(f"[yellow]{result.get('answer')}[/yellow]")
        console.print(f"[dim](Refused: {reason})[/dim]")
        return

    console.print(f"[bold green]{result['answer']}[/bold green]\n")
    console.print("[bold]Sources:[/bold]")
    for c in result.get("citations", []):
        loc = f" — {c['source_location']}" if c.get("source_location") else ""
        title = c.get("document_title") or c["document_id"]
        console.print(f"  [{c['n']}] {title}{loc}")


# ---------- Sources (connectors) ----------

@main.group()
def sources() -> None:
    """Manage data source connections (Notion, SharePoint, HubSpot, Jira)."""


@sources.command(name="list")
@click.pass_context
def sources_list(ctx: click.Context) -> None:
    """List configured data sources."""
    client: RAGClient = ctx.obj["client"]
    items = client.sources()
    if not items:
        console.print("[yellow]No sources configured.[/yellow]")
        return

    table = Table(title="Sources")
    table.add_column("Name", style="cyan")
    table.add_column("Type")
    table.add_column("Visibility")
    table.add_column("Documents", justify="right")
    table.add_column("Sync status")
    table.add_column("ID", style="dim")
    for s in items:
        table.add_row(
            s["name"], s["source_type"], s.get("default_visibility", "inherit"),
            str(s["document_count"]), s["sync_status"], s["id"][:8] + "...",
        )
    console.print(table)


@sources.command(name="connect")
@click.argument("source_type")
@click.argument("name")
@click.option("--config-json", default=None, help="Connection config as a JSON object")
@click.option("--token", default=None, help="Shortcut for {\"token\": ...} config")
@click.option("--no-auto-index", is_flag=True, help="Don't sync immediately after connecting")
@click.option("--visibility", type=click.Choice(["inherit", "restricted"]), default=None)
@click.option("--sync-query", default="*")
@click.option("--sync-limit", default=50)
@click.pass_context
def sources_connect(
    ctx: click.Context,
    source_type: str,
    name: str,
    config_json: str | None,
    token: str | None,
    no_auto_index: bool,
    visibility: str | None,
    sync_query: str,
    sync_limit: int,
) -> None:
    """Connect a new source. By default runs an immediate sync and shows
    a data-quality/conflict snapshot ("connect and see problems now")."""
    client: RAGClient = ctx.obj["client"]
    config: dict[str, Any] = json_module.loads(config_json) if config_json else {}
    if token:
        config["token"] = token

    with console.status(f"Connecting {name}..."):
        result = client.connect_source(
            source_type, name, config=config, auto_index=not no_auto_index,
            sync_query=sync_query, sync_limit=sync_limit, default_visibility=visibility,
        )

    console.print(f"[green]✓ Connected:[/green] {result['name']} ({result['source_type']})")
    console.print(f"  ID: {result['id']}  |  Visibility: {result.get('default_visibility')}")
    report = result.get("indexing_report")
    if report:
        console.print(
            f"  Synced: {report['documents_synced']}  |  "
            f"Avg quality: {report.get('avg_quality_score', 'N/A')}  |  "
            f"New conflicts: {report['new_conflicts']}"
        )
        if report.get("critical_findings") or report.get("warning_findings"):
            console.print(
                f"  [yellow]Findings — critical: {report['critical_findings']}, "
                f"warning: {report['warning_findings']}[/yellow]"
            )
        for conflict in report.get("top_conflicts", []):
            console.print(
                f"  [red]⚠ {conflict['conflict_type']}[/red]: "
                f"{conflict.get('document_a_title')} vs {conflict.get('document_b_title')}"
            )


@sources.command(name="sync")
@click.argument("source_id")
@click.option("--query", default="*")
@click.option("--limit", default=50)
@click.pass_context
def sources_sync(ctx: click.Context, source_id: str, query: str, limit: int) -> None:
    """Trigger a sync for an existing source."""
    client: RAGClient = ctx.obj["client"]
    with console.status("Syncing..."):
        stats = client.sync_source(source_id, query=query, limit=limit)
    console.print(f"[green]✓ Synced:[/green] {stats.get('synced', 0)} documents")
    if stats.get("errors"):
        console.print(f"  [yellow]{len(stats['errors'])} error(s)[/yellow]")


@sources.command(name="visibility")
@click.argument("source_id")
@click.argument("default_visibility", type=click.Choice(["inherit", "restricted"]))
@click.option(
    "--grant", "grants", multiple=True,
    help="Principal to grant whole-source read access to (repeatable)",
)
@click.pass_context
def sources_visibility(
    ctx: click.Context, source_id: str, default_visibility: str, grants: tuple[str, ...],
) -> None:
    """Admin-only: set a source's default visibility and optionally grant
    whole-source access to one or more principals."""
    client: RAGClient = ctx.obj["client"]
    result = client.set_source_visibility(
        source_id, default_visibility, grant_principal_ids=list(grants) or None,
    )
    console.print(f"[green]✓ Visibility updated:[/green] {result['default_visibility']}")
    if result.get("grants_added"):
        console.print(f"  Granted: {', '.join(result['grants_added'])}")


# ---------- Admin (tenant opt-in + introspection) ----------

@main.group()
def admin() -> None:
    """Admin API: tenant opt-in and access introspection."""


@admin.command(name="enable")
@click.pass_context
def admin_enable(ctx: click.Context) -> None:
    """Turn on the Admin API for this tenant. Requires the tenant's own
    master API key (not a personal key)."""
    client: RAGClient = ctx.obj["client"]
    client.admin_enable()
    console.print("[green]✓ Admin API enabled for this tenant.[/green]")


@admin.command(name="status")
@click.pass_context
def admin_status_cmd(ctx: click.Context) -> None:
    """Check whether the Admin API is enabled and whether this key is
    admin-capable."""
    client: RAGClient = ctx.obj["client"]
    status = client.admin_status()
    console.print(f"  Admin API enabled: {status['admin_api_enabled']}")
    console.print(f"  This key is admin-capable: {status['caller_is_admin_capable']}")
    console.print(f"  This key is legacy/master: {status['caller_is_legacy']}")


@admin.command(name="access")
@click.argument("principal_id")
@click.pass_context
def admin_access(ctx: click.Context, principal_id: str) -> None:
    """Show a principal's source-level effective access ("what can
    user:jane see")."""
    client: RAGClient = ctx.obj["client"]
    result = client.effective_access(principal_id)
    resolved = ", ".join(result["resolved_principal_ids"])
    console.print(f"[bold]{principal_id}[/bold] resolves to: {resolved}\n")

    table = Table(title="Source access")
    table.add_column("Source")
    table.add_column("Type")
    table.add_column("Visibility")
    table.add_column("Visible")
    table.add_column("Via")
    for s in result["sources"]:
        visible = "[green]yes[/green]" if s["visible"] else "[red]no[/red]"
        table.add_row(s["name"], s["source_type"], s["default_visibility"], visible, s["via"])
    console.print(table)


# ---------- Users (personal API keys) ----------

@main.group()
def users() -> None:
    """Manage personal API keys bound to a principal_id (RBAC identities)."""


@users.command(name="create")
@click.argument("name")
@click.argument("principal_id")
@click.option("--clearance", default="public", help="public, internal, confidential, secret")
@click.option("--admin", "is_admin", is_flag=True, help="Grant admin-API privileges")
@click.pass_context
def users_create(
    ctx: click.Context, name: str, principal_id: str, clearance: str, is_admin: bool,
) -> None:
    """Issue a personal API key, e.g. `raasoa users create Jane user:jane`.
    The full key is shown once — save it now."""
    client: RAGClient = ctx.obj["client"]
    result = client.create_user_key(name, principal_id, clearance=clearance, is_admin=is_admin)
    console.print(f"[green]✓ Key created for {result['principal_id']}:[/green]")
    console.print(f"  [bold]{result['key']}[/bold]")
    console.print("  [dim]This is shown only once — store it now.[/dim]")


@users.command(name="list")
@click.pass_context
def users_list(ctx: click.Context) -> None:
    """List API keys with their principal_id/clearance/is_admin."""
    client: RAGClient = ctx.obj["client"]
    keys = client.user_keys()
    if not keys:
        console.print("[yellow]No keys found.[/yellow]")
        return

    table = Table(title="API Keys")
    table.add_column("Name")
    table.add_column("Principal")
    table.add_column("Clearance")
    table.add_column("Admin")
    table.add_column("Active")
    table.add_column("Prefix", style="dim")
    for k in keys:
        table.add_row(
            k["name"], k.get("principal_id") or "[dim]legacy[/dim]", k["clearance"],
            "yes" if k["is_admin"] else "no", "yes" if k["is_active"] else "no", k["key_prefix"],
        )
    console.print(table)


# ---------- Groups (principal groups + memberships) ----------

@main.group()
def groups() -> None:
    """Manage principal groups and memberships."""


@groups.command(name="create")
@click.argument("principal_id")
@click.option("--display-name", default=None)
@click.pass_context
def groups_create(ctx: click.Context, principal_id: str, display_name: str | None) -> None:
    """Create a group, e.g. `raasoa groups create group:sales`."""
    client: RAGClient = ctx.obj["client"]
    result = client.create_group(principal_id, display_name=display_name)
    console.print(f"[green]✓ Group created:[/green] {result['principal_id']}")


@groups.command(name="list")
@click.pass_context
def groups_list(ctx: click.Context) -> None:
    """List groups."""
    client: RAGClient = ctx.obj["client"]
    items = client.groups()
    if not items:
        console.print("[yellow]No groups found.[/yellow]")
        return
    table = Table(title="Groups")
    table.add_column("Principal ID", style="cyan")
    table.add_column("Display name")
    table.add_column("Origin")
    for g in items:
        table.add_row(g["principal_id"], g.get("display_name") or "—", g["origin"])
    console.print(table)


@groups.command(name="delete")
@click.argument("principal_id")
@click.pass_context
def groups_delete(ctx: click.Context, principal_id: str) -> None:
    """Delete a group and its memberships."""
    client: RAGClient = ctx.obj["client"]
    client.delete_group(principal_id)
    console.print(f"[green]✓ Deleted group:[/green] {principal_id}")


@groups.command(name="members")
@click.argument("group_principal_id")
@click.pass_context
def groups_members(ctx: click.Context, group_principal_id: str) -> None:
    """List a group's members."""
    client: RAGClient = ctx.obj["client"]
    members = client.group_members(group_principal_id)
    if not members:
        console.print("[yellow]No members.[/yellow]")
        return
    for m in members:
        console.print(f"  {m['member_principal_id']}")


@groups.command(name="add-member")
@click.argument("group_principal_id")
@click.argument("member_principal_id")
@click.pass_context
def groups_add_member(
    ctx: click.Context, group_principal_id: str, member_principal_id: str,
) -> None:
    """Add a user or group to a group."""
    client: RAGClient = ctx.obj["client"]
    client.add_group_member(group_principal_id, member_principal_id)
    console.print(f"[green]✓ Added {member_principal_id} to {group_principal_id}[/green]")


@groups.command(name="remove-member")
@click.argument("group_principal_id")
@click.argument("member_principal_id")
@click.pass_context
def groups_remove_member(
    ctx: click.Context, group_principal_id: str, member_principal_id: str,
) -> None:
    """Remove a member from a group."""
    client: RAGClient = ctx.obj["client"]
    client.remove_group_member(group_principal_id, member_principal_id)
    console.print(f"[green]✓ Removed {member_principal_id} from {group_principal_id}[/green]")


# ---------- CRM (structured query path) ----------

def _parse_crm_filter(raw: str) -> dict[str, Any]:
    """Parse `field:op[:value]`, e.g. `amount:gte:10000` or
    `dealstage:in:closedwon,closedlost` or `hubspot_owner_id:is_null`."""
    parts = raw.split(":", 2)
    if len(parts) < 2:
        raise click.BadParameter(f"Filter must be field:op[:value] — got {raw!r}")
    field, op = parts[0], parts[1]
    value: Any = parts[2] if len(parts) > 2 else None
    if op == "in" and value is not None:
        value = [v.strip() for v in value.split(",")]
    elif op in ("gt", "gte", "lt", "lte") and value is not None:
        try:
            value = float(value) if "." in value else int(value)
        except ValueError as e:
            raise click.BadParameter(f"{op} requires a numeric value, got {value!r}") from e
    return {"field": field, "op": op, "value": value}


@main.group()
def crm() -> None:
    """Structured CRM query path (typed filters, not free-text search)."""


@crm.command(name="query")
@click.argument("object_type", type=click.Choice(["deals", "contacts", "companies", "tickets"]))
@click.option(
    "--filter", "raw_filters", multiple=True,
    help="field:op[:value], repeatable. op: eq,ne,gt,gte,lt,lte,in,contains,is_null",
)
@click.option("--sort", default=None, help="field:asc or field:desc")
@click.option("--limit", default=50)
@click.pass_context
def crm_query_cmd(
    ctx: click.Context,
    object_type: str,
    raw_filters: tuple[str, ...],
    sort: str | None,
    limit: int,
) -> None:
    """Filter CRM records, e.g.:
    `raasoa crm query deals --filter dealstage:eq:closedwon --filter amount:gte:10000`
    """
    client: RAGClient = ctx.obj["client"]
    filters = [_parse_crm_filter(f) for f in raw_filters]
    sort_spec: dict[str, str] | None = None
    if sort:
        sort_field, _, sort_dir = sort.partition(":")
        sort_spec = {"field": sort_field, "direction": sort_dir or "asc"}

    result = client.crm_query(object_type, filters=filters, sort=sort_spec, limit=limit)
    records = result.get("results", [])
    if not records:
        console.print(f"[yellow]No {object_type} matched.[/yellow]")
        return

    console.print(f"[bold]{result['count']} {object_type} matched:[/bold]\n")
    for r in records:
        props = r.get("properties") or {}
        summary = ", ".join(f"{k}={v}" for k, v in props.items() if v not in (None, ""))
        console.print(f"  [{r['external_id']}] {summary}")


def run() -> None:
    """Console-script entry point — wraps main() so an API error (bad
    input rejected by the server, auth failure, connection refused)
    prints a clean message instead of a raw Python traceback."""
    try:
        main()
    except httpx.HTTPStatusError as e:
        detail: Any = e.response.text
        import contextlib

        with contextlib.suppress(Exception):
            detail = e.response.json().get("detail", detail)
        console.print(f"[red]API error ({e.response.status_code}):[/red] {detail}")
        sys.exit(1)
    except httpx.ConnectError as e:
        console.print(f"[red]Connection failed:[/red] {e}")
        sys.exit(1)


if __name__ == "__main__":
    run()
