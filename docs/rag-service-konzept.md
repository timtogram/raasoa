# Enterprise RAG as a Service / API

## Executive Summary

Ziel ist kein weiteres schweres "AI Knowledge Platform"-Monster, sondern ein kleiner, belastbarer Retrieval-Service mit klarer Trennung zwischen:

- **Ingestion und Qualitätskontrolle**
- **versioniertem Wissensspeicher**
- **zuverlässiger Retrieval-API mit dualem Query-Pfad**
- **optionaler MCP-Fassade für Bots und Agenten**

### Zentrale Architekturentscheidungen

1. **PostgreSQL als einzige Datenbank** -- System of Record, Vektorspeicher (pgvector), Full-Text-Search (tsvector) und Change Detection in einem System. Keine separate Vector DB nötig.

2. **Tiered Indexing** -- Nicht alles wird embedded. Nur aktiv genutzte Dokumente (10-20%) liegen als volle Chunk-Embeddings im Vektorspeicher. Der Rest wird über BM25 oder on-demand abgedeckt.

3. **Dualer Query-Pfad** -- Ein Query Router trennt zwischen semantischem Wissens-Retrieval (RAG) und strukturierten Datenabfragen (SQL/API gegen ERP/CRM). Beide Muster laufen in einem System.

4. **Cloud- und lokal-fähig** -- Embedding- und Reranking-Modelle sind über ein Provider-Interface abstrahiert. Derselbe Service läuft mit Cloud-APIs oder lokalen Modellen.

5. **MCP ist Adapter, nicht Kern** -- Der Kern ist ein eigenständiger Service mit stabiler REST/gRPC-API. MCP setzt als dünne Fassade darauf auf.

Wenn das Produkt in Unternehmen zuverlässig funktionieren soll, reichen "Connector + Vector DB + LLM" nicht aus. Es braucht zusätzlich:

- deterministische und idempotente Ingestion
- sichtbare Versionierung bis auf Dokument- und Chunk-Ebene
- ACL-Propagation
- Qualitätsmetriken beim Indexieren
- Konflikt- und Widerspruchserkennung
- Human-in-the-Loop für Korrekturen
- reproduzierbare Retrieval-Ergebnisse mit Audit-Spur

## Warum das trotz großer Kontextfenster relevant bleibt

Große Kontextfenster lösen einige Probleme, aber nicht die Kernprobleme im Unternehmen:

- **Aktualität:** Modelle kennen neue Dokumentstände nicht.
- **Berechtigungen:** Kontextfenster erzwingen keine ACLs.
- **Quelltransparenz:** Unternehmen wollen belastbare Zitate, Versionen und Nachvollziehbarkeit.
- **Unvollständige Daten:** Viele Quellen enthalten Altstände, Dubletten, Anhänge, Makros, Tabellen, schlecht formatierten Text oder widersprüchliche Inhalte.
- **Betrieb:** Es braucht Delta-Sync, Fehlerbehandlung, Wiederanlauf, Monitoring und Freigabeprozesse.
- **Strukturierte Daten:** Fragen wie "Wie viele Rechnungen hat Kunde X?" brauchen SQL-Zugriff auf ERP/CRM, keine Vektorsuche.

Die Lösung ist daher kein "größerer Prompt", sondern ein **retrieval-zentriertes Wissenssystem mit Governance und dualem Query-Pfad**.

## Zielbild

Der Service soll drei Modi abdecken:

1. **Managed zentral bei euch**
2. **Dedicated Deployment pro Kunde**
3. **On-Prem / air-gapped beim Kunden**

Funktional liefert er:

- Konnektoren zu SharePoint, Jira, Confluence und weiteren Systemen
- inkrementelles und auditierbares Indexieren mit Content-Hash-basierter Change Detection
- struktur- und qualitätsbewusste Dokumentaufbereitung
- hybride Suche (dense + BM25) mit Re-Ranking in PostgreSQL
- strukturierte Datenabfragen gegen ERP/CRM über Tool-Calling
- versionierte, zitierfähige Ergebnisse
- Konflikt-/Widerspruchshinweise
- Review-Queues für Menschen
- API-first und optional MCP-first Access

## Produktprinzipien

1. **PostgreSQL als einzige Datenbank**
   Kein Qdrant, kein Elastic im Kern. PostgreSQL mit pgvector und tsvector liefert Vektorsuche, Full-Text-Search, relationales Datenmodell, Transaktionen und Change Detection in einem System. Weniger Dependencies = einfacheres Deployment, besonders on-prem.

2. **Tiered Indexing statt "alles embedden"**
   Der Vektorspeicher ist ein Cache für Retrieval, nicht die Quelle der Wahrheit. Nur aktiv genutzte Dokumente werden voll embedded (Hot-Tier). Der Rest wird über BM25 abgedeckt oder on-demand embedded.

3. **System of Record vor Vector Store**
   Retrieval darf nie der einzige Wahrheitslayer sein. Der kanonische Zustand gehört in das relationale Modell plus Objekt-Storage.

4. **Top-Connectoren nativ, Long Tail per Framework**
   Für SharePoint, Jira und Confluence sollte die Synchronisationslogik selbst kontrolliert werden. Für den Long Tail darf ein Connector-Framework helfen.

5. **Chunking ist kein Detail, sondern Kern-IP**
   Falsches Chunking zerstört Retrieval-Qualität. Tabellen, Anhänge, Makros, Überschriften und Versionen müssen bewusst behandelt werden.

6. **Abstention vor Halluzination**
   Wenn Qualität, Vollständigkeit oder Konsistenz nicht ausreichen, muss das System Unsicherheit sichtbar machen oder nur Evidenz liefern.

7. **Reviewbarkeit als Produktfeature**
   Menschliche Korrekturen dürfen nicht außerhalb des Systems stattfinden, sondern müssen direkt in den Index-Lifecycle zurückfließen.

8. **Cloud- und lokal-fähig ab Tag 1**
   Embedding- und Reranking-Modelle sind über ein Provider-Interface abstrahiert. Kein Hard-Lock auf einen Cloud-Anbieter.

## Kernanforderungen

### Funktional

- Delta- und Vollsynchronisation mit Content-Hash-basierter Change Detection
- Dokument-, Anhangs- und Kommentarverarbeitung
- Rechteübernahme aus Quellsystemen
- strukturierte und unstrukturierte Inhalte
- semantische und keyword-basierte Suche (Hybrid Search in PostgreSQL)
- strukturierte Datenabfragen gegen ERP/CRM (Query Router)
- versionierte Dokument- und Chunk-Retrieval-API
- Konflikterkennung
- Review- und Korrekturworkflow

### Nicht-funktional

- on-prem-fähig (minimal: PostgreSQL + MinIO + API)
- fehlertolerant und idempotent
- mandantenfähig
- revisionssicher auditierbar
- observability-fähig
- modell-agnostisch (Cloud und lokale Modelle über Provider-Interface)

## Referenzarchitektur

### 1. Source Plane

Hier sitzen Konnektoren und Checkpointer.

Komponenten:

- SharePoint Connector
- Jira Connector
- Confluence Connector
- Connector SDK für weitere Quellen
- Scheduler/Webhook-Empfänger
- Source-specific checkpoint store (in PostgreSQL)

Verantwortung:

- Quellen authentifizieren
- Änderungen erkennen (Delta Tokens, Webhooks, Polling)
- Quellobjekte mit stabilen IDs erfassen
- ACLs, Metadaten, Versionen und Änderungsereignisse extrahieren
- Rohdaten unverändert in Objekt-Storage archivieren
- Sync-Cursors und Change Events in PostgreSQL schreiben

### 2. Processing Plane

Hier wird aus Rohdaten ein belastbares Wissensobjekt.

Komponenten:

- Normalizer
- Parser/Partitioner
- Attachment Resolver
- Chunker
- Embedding Pipeline (über Provider-Interface)
- Quality Gate Engine
- Conflict Detection Engine

Verantwortung:

- verschiedene Formate in ein kanonisches Dokumentmodell überführen
- Anhänge und eingebettete Inhalte auflösen
- Tabellen, Titel, Listen, Kommentare und Metadaten erhalten
- Chunks erzeugen und Content-Hashes berechnen
- selektiv Embeddings erzeugen (nur für geänderte Chunks, nur für Hot-/Warm-Tier)
- Qualitätsmetriken berechnen
- Reviewfälle erzeugen

### 3. Knowledge Plane

Das ist die eigentliche Wissensbasis.

**Einzige Datenbank: PostgreSQL** mit folgenden Erweiterungen:

- **pgvector** für dense Vektorsuche (HNSW-Index)
- **tsvector** für Full-Text-Search / BM25

**Speicher:**

- **PostgreSQL** als System of Record, Vektorspeicher und Change Detection Layer
- **S3/MinIO** für Rohartefakte und extrahierte Artefakte

In PostgreSQL liegen:

- tenants, sources, source_objects
- documents, document_versions, document_sections
- chunks, chunk_versions (mit `content_hash` und optional `embedding`)
- acl_entries
- sync_cursors, change_events
- ingestion_runs, quality_findings
- conflict_candidates, review_tasks, corrections
- retrieval_logs
- structured_sources (ERP/CRM-Anbindungen für Query Router)
- structured_query_log

Im Objekt-Storage liegen:

- Rohdokumente
- extrahierte Anhänge
- Parser-Artefakte
- Screenshots/OCR-Artefakte falls nötig

#### Warum PostgreSQL + pgvector statt einer separaten Vector DB

| Kriterium | pgvector | Separate Vector DB (Qdrant, etc.) |
|-----------|----------|-----------------------------------|
| Vektormenge <10M | Ausreichend (<5ms p50) | Überqualifiziert |
| Hybrid Search | tsvector + pgvector in einer SQL-Query | Separate API-Calls |
| Transaktionale Konsistenz | Vektoren + Metadaten atomar | Eventual Consistency |
| On-Prem Complexity | Eine Dependency | Zwei Systeme, doppeltes Ops |
| Filtered Search | Seit pgvector 0.8 iterative scan (5.7x besser) | Stärker bei komplexen Filtern |
| Concurrent Writes | Schwächer (~20 rows/sec HNSW) | Stärker (Segment-Architektur) |
| Quantization | halfvec (2x) | Bis zu 32x |
| ColBERT/Multi-Vector | Nicht nativ | Möglich |

**Entscheidung:** pgvector reicht für Phase 1+2. Bei >10M Vektoren oder speziellen Anforderungen kann Qdrant als optionaler Retrieval-Accelerator nachgerüstet werden -- das Tiered-Modell (PostgreSQL als Wahrheit, Vektorspeicher als Cache) macht den Wechsel sauber.

**Skalierungspfad:** pgvector → pgvectorscale (DiskANN, 471 QPS bei 99% Recall auf 50M Vektoren) → optionale separate Vector DB.

### 4. Serving Plane

Drei Frontdoors:

- **REST/gRPC API** für Anwendungen
- **MCP Server** für Agents/Bot-Systeme
- **Query Router** trennt zwischen RAG-Retrieval und strukturierten Datenabfragen

REST/gRPC ist die primäre fachliche API.
MCP ist die Interoperabilitätsschicht.
Der Query Router ist intern und entscheidet pro Query, welcher Pfad genutzt wird.

### 5. Governance Plane

Querschnittsfunktionen:

- Audit Log
- OpenTelemetry Tracing
- Review UI
- Admin UI
- Metrics/Evaluation
- Policy Engine

## Datenspeicher-Strategie: Tiered Indexing

### Kernprinzip

Nicht alles wird in den Vektorspeicher geschrieben. PostgreSQL hält die Wahrheit, der Vektorspeicher (pgvector) enthält nur das, was aktiv für Retrieval gebraucht wird.

### Drei Tiers

| Tier | Was liegt in pgvector? | Retrieval-Methode | Latenz | Anteil am Corpus |
|------|------------------------|-------------------|--------|-----------------|
| **Hot** | Volle Chunk-Embeddings (HNSW) | Dense + BM25 Hybrid | <50ms | ~10-20% |
| **Warm** | Nur Dokument-Summary-Embedding | Summary als Pointer, Chunks on-demand | ~200ms | ~20-30% |
| **Cold** | Nichts | BM25 / tsvector Full-Text, Embedding on-demand | ~500ms | ~50-70% |

### Ingestion-Verhalten pro Tier

**Bei der Ingestion:**

1. Dokument wird synchronisiert, Content-Hash berechnet, Rohdaten in S3 gespeichert
2. Dokument wird gechunkt, Chunk-Hashes in PostgreSQL gespeichert
3. tsvector (Full-Text-Index) wird für alle Tiers erzeugt
4. Embedding passiert nur für Hot-Tier-Dokumente

**Bei einem Update:**

1. Delta-Sync erkennt Änderung
2. Neuer Content-Hash wird gegen PostgreSQL verglichen
3. Wenn sich der Hash geändert hat: Dokument wird neu gechunkt
4. Chunk-Hashes werden verglichen -- nur geänderte Chunks werden neu embedded
5. Unveränderte Chunks behalten ihre bestehenden Vektoren

### Tier-Promotion: Adaptives Indexing

Dokumente starten kalt und werden bei Bedarf promoted:

```
Cold (nur Metadaten + tsvector Full-Text)
  |
  | Dokument wird abgefragt → on-demand Embedding + Cache
  v
Warm (Summary-Embedding als Pointer)
  |
  | Häufig abgefragt (Schwellwert überschritten)
  v
Hot (volle Chunk-Embeddings in pgvector)
  |
  | Nicht mehr abgefragt (Decay-Periode)
  v
Warm → Cold (Embeddings evicten)
```

### Reduktion

80-90% weniger Vektorspeicher bei gleichwertiger Retrieval-Qualität für die Mehrheit der Queries.

In Enterprise-Umgebungen folgen Zugriffsmuster einer Pareto-Verteilung: 10-20% der Dokumente erzeugen 80%+ der Queries.

## Change Detection Architektur

### Kernprinzip

Change Detection passiert ausschließlich in PostgreSQL über Content-Hashes und Provider-native Delta-Mechanismen. Der Vektorspeicher wird nie für Change Detection befragt.

### Provider-native Mechanismen

| Quelle | Primärer Mechanismus | Fallback |
|--------|---------------------|----------|
| **SharePoint** | `driveItem/delta` + Webhooks | Polling alle 6-24h |
| **Confluence** | CQL `lastmodified > {timestamp}` | Voller Crawl periodisch |
| **Jira** | Webhooks + `changelog` | JQL `updated >= -7d` |

### Change Detection Flow

```
1. Webhook/Delta → Änderung erkannt → change_events in PostgreSQL
2. Worker → FOR UPDATE SKIP LOCKED auf change_events → parallele Verarbeitung
3. Content-Hash Vergleich → nur bei tatsächlicher Inhaltsänderung weiter
4. Re-Chunking → Chunk-Hashes vergleichen
5. Nur geänderte Chunks neu embedden
```

### Selektives Re-Embedding

```
Dokument geändert
    |
    v
Re-Chunking (gleiche Strategie wie Original)
    |
    v
Für jeden neuen Chunk: SHA-256(chunk_text) berechnen
    |
    v
Vergleich mit gespeichertem content_hash in PostgreSQL
    |
    +-- Hash gleich → Skip (kein Re-Embedding)
    +-- Hash anders → Re-Embed, Vektor in pgvector updaten
    +-- Neuer Chunk (kein Match by Index) → Embed + Insert
    +-- Chunk weggefallen → Aus pgvector löschen
```

**Ergebnis:** Wenn sich in einem 50-Seiten-Dokument ein Absatz ändert, werden 1-2 von 200 Chunks neu embedded -- nicht 200.

### Realistische SLOs

| Metrik | Ziel |
|--------|------|
| Change Detection | < 5 Minuten (Webhook) |
| Re-Embedding | < 15 Minuten |
| End-to-End Freshness | < 30 Minuten |
| Fallback Full-Sync | Alle 6-24 Stunden |

## Query Router: Dualer Retrieval-Pfad

### Warum zwei Pfade nötig sind

Enterprise-Fragen fallen in zwei fundamental verschiedene Kategorien:

**Typ A: Wissens-/Verständnisfrage (RAG)**
> "Welches Wissen ist für Service-Techniker relevant bei Fehlercode E-4021?"

- Unstrukturierter Text (Handbücher, Confluence, Jira-Tickets)
- Antwort liegt verteilt über mehrere Chunks
- Braucht: Embedding + Hybrid Search + Reranking

**Typ B: Fakten-/Aggregationsfrage (Structured Query)**
> "Wie viele Rechnungen haben wir für Kunde Müller GmbH geschrieben?"

- Strukturierte Daten (ERP, CRM, Datenbank)
- Antwort ist eine exakte Zahl/Aggregation
- Braucht: SQL-Query oder API-Call, keine Vektorsuche

**Typ C: Hybrid (beides)**
> "Zeige mir alle Reklamationen von Kunde X und was die Service-Doku dazu sagt"

- Parallel: Structured Query + RAG
- Ergebnisse werden zusammengeführt

### Architektur

```
User Query
    |
    v
[Query Classifier / Router]
    |
    +→ Typ A: Wissensfrage
    |    → RAG Pipeline (Hybrid Search + Reranking)
    |    → "Laut Servicehandbuch v3.2, Seite 47..."
    |
    +→ Typ B: Faktenfrage
    |    → Structured Query Pipeline
    |    → Tool-Calling gegen ERP/CRM
    |    → "Kunde X: 47 Rechnungen, Gesamtwert 234.500 EUR"
    |
    +→ Typ C: Hybrid
         → Parallel: RAG + Structured Query
         → Ergebnisse zusammengeführt
```

### Structured Query Path

Zwei Implementierungsstufen:

**Stufe 1 (MVP): Vordefinierte Tools**

Sicherer Ansatz -- kein Risiko durch LLM-generiertes SQL:

```python
# Tool-Definitionen pro Tenant
tools = [
    Tool(name="get_customer_invoices",
         params={"customer_id": int},
         handler=erp_api.get_invoices),
    Tool(name="get_machine_history",
         params={"machine_id": str},
         handler=erp_api.get_machine_history),
    Tool(name="get_ticket_stats",
         params={"project": str, "status": str},
         handler=jira_api.get_stats),
]
```

Der LLM wählt das richtige Tool und extrahiert die Parameter aus der Frage.

**Stufe 2 (später): Text-to-SQL**

Für komplexere Ad-hoc-Abfragen, mit Schema-Beschreibung als Kontext:

```
"Welche Kunden hatten letztes Quartal mehr als 5 Reklamationen?"
    |
    v
[LLM generiert SQL basierend auf Schema-Beschreibung]
    |
    v
SELECT c.name, COUNT(*) as claims
FROM claims cl JOIN customers c ON cl.customer_id = c.id
WHERE cl.created_at >= '2025-10-01'
GROUP BY c.name HAVING COUNT(*) > 5
```

### Datenmodell für Structured Sources

```sql
CREATE TABLE structured_sources (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    name                TEXT NOT NULL,
    source_type         TEXT NOT NULL,       -- 'sql_database', 'rest_api'
    connection_config   JSONB NOT NULL,      -- verschlüsselt
    schema_description  TEXT,                -- natürlichsprachig, für LLM-Kontext
    available_tools     JSONB,               -- Tool-Definitionen
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE structured_query_log (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    user_query      TEXT NOT NULL,
    routed_to       TEXT NOT NULL,           -- 'rag', 'structured', 'hybrid'
    tool_called     TEXT,
    parameters      JSONB,
    result_summary  TEXT,
    executed_at     TIMESTAMPTZ DEFAULT now()
);
```

## Datenmodell

### Konkretes SQL-Schema

```sql
-- ============================================================
-- Tenants und Sources
-- ============================================================

CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    config          JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE sources (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    source_type     TEXT NOT NULL,           -- 'sharepoint', 'confluence', 'jira'
    name            TEXT NOT NULL,
    connection_config JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Sync-State und Change Detection
-- ============================================================

CREATE TABLE sync_cursors (
    source_type     TEXT NOT NULL,
    source_id       UUID NOT NULL REFERENCES sources(id),
    delta_token     TEXT,                    -- opaque Cursor vom Provider
    last_sync_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sync_status     TEXT DEFAULT 'idle',     -- 'idle', 'in_progress', 'failed'
    error_message   TEXT,
    items_synced    INTEGER DEFAULT 0,
    PRIMARY KEY (source_type, source_id)
);

CREATE TABLE change_events (
    id              BIGSERIAL PRIMARY KEY,
    document_id     UUID NOT NULL,
    event_type      TEXT NOT NULL,           -- 'created', 'updated', 'deleted'
    source_event_id TEXT,                    -- Webhook Delivery ID oder Delta Token
    old_content_hash BYTEA,
    new_content_hash BYTEA,
    chunks_affected INTEGER,
    detected_at     TIMESTAMPTZ DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX idx_events_unprocessed
    ON change_events (detected_at)
    WHERE processed_at IS NULL;

-- ============================================================
-- Dokumente und Versionierung
-- ============================================================

CREATE TABLE documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    source_id       UUID NOT NULL REFERENCES sources(id),
    source_object_id TEXT NOT NULL,          -- externe ID aus Quellsystem
    source_url      TEXT,
    content_hash    BYTEA,                   -- SHA-256 des Gesamtdokuments
    title           TEXT,
    doc_type        TEXT,                    -- 'page', 'issue', 'file', etc.
    last_modified   TIMESTAMPTZ,             -- aus Quellsystem
    last_synced_at  TIMESTAMPTZ,
    last_embedded_at TIMESTAMPTZ,
    embedding_model TEXT,                    -- z.B. 'cohere-embed-v4'
    chunk_count     INTEGER DEFAULT 0,
    version         INTEGER DEFAULT 1,
    index_tier      TEXT DEFAULT 'cold',     -- 'hot', 'warm', 'cold'
    access_count    INTEGER DEFAULT 0,       -- für Tier-Promotion
    last_accessed_at TIMESTAMPTZ,
    status          TEXT DEFAULT 'pending',  -- 'pending', 'processing', 'indexed', 'error'
    review_status   TEXT DEFAULT 'auto_published',
    quality_score   REAL,
    conflict_status TEXT DEFAULT 'none',     -- 'none', 'potential', 'confirmed'
    UNIQUE (tenant_id, source_id, source_object_id)
);

CREATE INDEX idx_docs_status ON documents (status) WHERE status != 'indexed';
CREATE INDEX idx_docs_tier ON documents (tenant_id, index_tier);
CREATE INDEX idx_docs_source ON documents (source_id, source_object_id);

CREATE TABLE document_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    content_hash    BYTEA NOT NULL,
    source_version  TEXT,                    -- ETag, Revision, etc.
    parser_version  TEXT,
    chunking_strategy_version TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (document_id, version)
);

-- ============================================================
-- Chunks mit Content-Hash-basierter Change Detection
-- ============================================================

CREATE TABLE chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    content_hash    BYTEA NOT NULL,          -- SHA-256 des Chunk-Texts
    chunk_text      TEXT NOT NULL,
    section_title   TEXT,                    -- Überschrift des Abschnitts
    chunk_type      TEXT DEFAULT 'text',     -- 'text', 'table', 'code', 'comment'
    token_count     INTEGER,
    embedding       vector(1024),            -- pgvector; NULL wenn Cold-Tier
    embedding_model TEXT,
    embedded_at     TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}',
    tsv             tsvector,                -- Full-Text-Index für BM25
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX idx_chunks_embedding ON chunks
    USING hnsw (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;

CREATE INDEX idx_chunks_tsv ON chunks USING gin (tsv);

CREATE INDEX idx_chunks_document ON chunks (document_id);

CREATE INDEX idx_chunks_needs_embedding ON chunks (document_id)
    WHERE embedded_at IS NULL AND embedding IS NULL;

-- ============================================================
-- ACLs
-- ============================================================

CREATE TABLE acl_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    principal_type  TEXT NOT NULL,           -- 'user', 'group', 'role'
    principal_id    TEXT NOT NULL,
    permission      TEXT NOT NULL,           -- 'read', 'write'
    source_acl_id   TEXT,                    -- ID im Quellsystem
    synced_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_acl_document ON acl_entries (document_id);
CREATE INDEX idx_acl_principal ON acl_entries (principal_type, principal_id);

-- ============================================================
-- Quality und Governance
-- ============================================================

CREATE TABLE ingestion_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       UUID NOT NULL REFERENCES sources(id),
    started_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    status          TEXT DEFAULT 'running',
    documents_processed INTEGER DEFAULT 0,
    chunks_created  INTEGER DEFAULT 0,
    chunks_embedded INTEGER DEFAULT 0,
    errors          JSONB DEFAULT '[]'
);

CREATE TABLE quality_findings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id),
    finding_type    TEXT NOT NULL,           -- 'missing_title', 'low_text', 'ocr_failed', etc.
    severity        TEXT NOT NULL,           -- 'info', 'warning', 'error'
    details         JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE conflict_candidates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    document_a_id   UUID NOT NULL REFERENCES documents(id),
    document_b_id   UUID NOT NULL REFERENCES documents(id),
    conflict_type   TEXT NOT NULL,           -- 'hard', 'soft', 'supersession', 'authority'
    confidence      REAL,
    details         JSONB,
    status          TEXT DEFAULT 'new',      -- 'new', 'confirmed', 'resolved', 'dismissed'
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE review_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL,
    document_id     UUID REFERENCES documents(id),
    conflict_id     UUID REFERENCES conflict_candidates(id),
    task_type       TEXT NOT NULL,           -- 'quality_review', 'conflict_review', 'correction'
    status          TEXT DEFAULT 'new',      -- 'new', 'in_progress', 'approved', 'rejected'
    assigned_to     TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ
);

CREATE TABLE corrections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id),
    chunk_id        UUID REFERENCES chunks(id),
    correction_type TEXT NOT NULL,
    original_text   TEXT,
    corrected_text  TEXT,
    reason          TEXT,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Retrieval Audit
-- ============================================================

CREATE TABLE retrieval_logs (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    query_text      TEXT NOT NULL,
    routed_to       TEXT NOT NULL,           -- 'rag', 'structured', 'hybrid'
    chunks_returned UUID[],
    retrieval_confidence REAL,
    answerable      BOOLEAN,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Structured Data Sources (für Query Router)
-- ============================================================

CREATE TABLE structured_sources (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    name                TEXT NOT NULL,
    source_type         TEXT NOT NULL,       -- 'sql_database', 'rest_api'
    connection_config   JSONB NOT NULL,
    schema_description  TEXT,
    available_tools     JSONB,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE structured_query_log (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL,
    user_query      TEXT NOT NULL,
    routed_to       TEXT NOT NULL,
    tool_called     TEXT,
    parameters      JSONB,
    result_summary  TEXT,
    executed_at     TIMESTAMPTZ DEFAULT now()
);
```

### Kanonische IDs

Jedes Objekt braucht stabile IDs:

- `tenant_id`
- `source_id`
- `source_object_id`
- `document_id`
- `document_version_id`
- `section_id`
- `chunk_id`

### Pflichtmetadaten pro Dokumentversion

- source type, source locator
- source-native version / etag / revision
- created_at, modified_at, indexed_at
- parser_version, chunking_strategy_version
- embedding_model (mit `content_hash` verknüpft)
- review_status, quality_score, conflict_status
- acl_fingerprint, content_hash

### Warum Content-Hashes auf Chunk-Ebene

Nur so könnt ihr:

- exakt reproduzieren, was indexiert wurde
- Treffer sauber zitieren
- Versionen sichtbar machen
- bei Änderungen nur betroffene Chunks neu embedden (statt alles)
- Parser-/Modellwechsel kontrolliert vergleichen
- Embedding-Modell-Migrationen planen (welche Chunks müssen re-embedded werden?)

## Embedding-Modell-Strategie

### Modell-Abstraction Layer

Die modellabhängigen Stellen sind genau drei:

1. **Embedding** (Ingestion + Query-Zeit)
2. **Reranking** (Query-Zeit)
3. **Claim Extraction / NLI** (Ingestion-Zeit, Phase 3)

Dafür ein einfaches Provider-Interface:

```python
class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...


class RerankProvider(Protocol):
    async def rerank(
        self, query: str, documents: list[str], top_k: int
    ) -> list[ScoredDocument]: ...
```

### Empfohlene Modelle

#### Embedding (für deutschen Enterprise-Content)

| Prio | Modell | Typ | Dimensionen | Stärke |
|------|--------|-----|-------------|--------|
| 1 | **Cohere Embed v4** | Cloud | 256-1536 (MRL) | Multilingual, 128K Context |
| 2 | **Qwen3-Embedding-8B** | Self-Hosted | 32-7168 (MRL) | Bestes Self-Hosted multilingual |
| 3 | **Jina v2 base-de** | Self-Hosted | 768 | Deutsch-spezialisiert, 8K Context |
| 4 | **BGE-M3** | Self-Hosted | 1024 | Open Source, Hybrid (dense+sparse+ColBERT) |

#### Reranking

| Prio | Modell | Typ | Impact |
|------|--------|-----|--------|
| 1 | **Cohere Rerank 4 Pro** | Cloud | +20-35% Accuracy |
| 2 | **BGE-Reranker / Jina Reranker v3** | Self-Hosted | Vergleichbar, für On-Prem |

### Implementierungen pro Deployment

| Provider | Cloud | Lokal |
|----------|-------|-------|
| Embedding | Cohere Embed v4 / OpenAI / Voyage | Qwen3-Embedding / Jina / BGE-M3 via TEI |
| Reranking | Cohere Rerank 4 / Jina | Cross-Encoder via TEI |

### Wichtige Regeln

1. **Embedding-Dimensionen in der Config, nicht im Code** -- pgvector HNSW-Index ist an Dimensionen gebunden.
2. **Model-ID an jedem Chunk speichern** -- `embedding_model` in der chunks-Tabelle. Bei Modellwechsel weiß man, welche Chunks re-embedded werden müssen.
3. **Batch-Verhalten intern im Provider** -- Cloud-APIs sind I/O-bound mit Rate Limits, lokale Modelle sind GPU-bound mit anderen optimalen Batch-Größen.
4. **Kein gemischter Embedding Space** -- Chunks mit Modell A sind nicht kompatibel mit Queries von Modell B. Migration = komplett re-embedden.
5. **Ein Tenant, ein Modell** -- Wechsel als geplante Migration, nicht zur Runtime.

### Deutsch-spezifische Hinweise

- Compound-Wörter ("Datenschutzgrundverordnung") werden von modernen Subword-Tokenizern akzeptabel behandelt
- Für domänenspezifische Compounds optional Decomposer vorschalten
- Multilingual-Modelle (Cohere, Qwen3, BGE-M3) sind für gemischt deutsch/englische Corpora besser als rein deutsche Modelle
- German ColBERT (2025) ist eine Option für Phase 3 Late Interaction

## MCP-Einordnung

### Warum MCP sinnvoll ist

MCP passt gut als standardisierte Zugriffsschicht für Bot-Systeme:

- Ressourcen können zitierfähige Dokumente, Versionen und Chunks exponieren.
- Tools können Suche, Versionenvergleich, Konfliktabfrage und Review-Aktionen anbieten.
- Streamable HTTP macht Serverbetrieb für mehrere Clients praktikabel.

### Warum MCP nicht der Kern sein sollte

MCP definiert nicht:

- euren Ingestion-Lifecycle
- Datenmodellierung
- Qualitätskontrolle
- Versionierungsmodell
- ACL-Strategie
- Persistenz
- Reliability Patterns

Deshalb:

**Kernservice zuerst, MCP-Adapter danach.**

### Sinnvolle MCP-Ressourcen

- `rag://tenant/{tenant_id}/document/{doc_id}`
- `rag://tenant/{tenant_id}/document/{doc_id}/version/{version_id}`
- `rag://tenant/{tenant_id}/chunk/{chunk_id}`
- `rag://tenant/{tenant_id}/conflict/{conflict_id}`

### Sinnvolle MCP-Tools

- `search_documents` -- semantische und keyword-basierte Suche
- `query_structured_data` -- strukturierte Abfragen (Rechnungen, Statistiken)
- `get_document`
- `get_document_version`
- `compare_versions`
- `list_conflicts`
- `submit_correction`
- `request_human_review`

### Empfehlung

Ressourcen für lesbare Evidenz, Tools für Aktionen.
Für sicherheitskritische Schreibaktionen immer mit expliziter Human-Freigabe.

## Connector-Strategie

### Tier 1: Native Enterprise Connectoren

Diese drei würde ich **selbst kontrolliert** bauen:

- SharePoint
- Jira
- Confluence

Grund:

- Dort hängen die wichtigsten Enterprise-Fälle
- Dort sind Rechte, Änderungen, Versionen und Anhänge geschäftskritisch
- Die Zuverlässigkeit der Delta-Sync-Logik ist ein Produktkern, kein Nebendetail

### SharePoint

Empfohlener Ansatz:

- Microsoft Graph für Datei- und Delta-Sync
- `driveItem/delta` als primärer Change-Detection-Mechanismus
- Change Notifications/Webhooks, wo praktikabel
- Fallback auf Polling alle 6-24h
- ACL- und Security-Event-Verarbeitung

Wichtig:

- `cTag` ändert sich bei Content-Änderungen -- nutzen um zu entscheiden ob Re-Download nötig
- Webhooks erfordern öffentlich erreichbaren HTTPS-Endpunkt; für On-Prem oft Polling realistischer
- Neue Implementierungen auf Entra ID App Registrations, nicht alte SharePoint App Principals
- Delta-Token immer transaktional mit den produzierten Änderungen speichern

### Jira

Empfohlener Ansatz:

- Webhooks für Änderungen (mit JQL-Filter: `"project = X AND updated >= -7d"`)
- Webhook-Payload enthält `changelog.items[]` mit `field`, `from`, `to`
- `expand=changelog` auf Issue GET für volle Änderungshistorie
- Attachments, Kommentare und Links explizit mitdenken

### Confluence

Empfohlener Ansatz:

- CQL Search für Discovery: `type=page AND lastmodified > "{timestamp}"`
- Content-Versionen explizit erfassen (`expand=version`)
- Attachments und eingebettete Dateien separat auflösen
- Body-Konvertierung und Makro-Edge-Cases bewusst behandeln
- `last_sync_timestamp` pro Space speichern, mit 5-Minuten-Overlap für Clock-Skew

Wichtig:

- Body-Expansion caps results at 50; ohne Body limit 1000
- Nicht nur Seiteninhalt indexieren, sondern auch Anhänge und deren Beziehungen
- Historische Versionen müssen im Modell sichtbar bleiben

### Tier 2: Connector Framework für den Long Tail

Für viele weitere Quellen würde ich **nicht alles nativ bauen**.

Sinnvoller Ansatz:

- eigenes Connector SDK
- Long-Tail-Anbindung über Frameworks wie **Unstructured** oder **Airbyte**

Empfehlung:

- **Unstructured** als Parsing-/Ingestion-Beschleuniger
- **Airbyte** für zusätzliche strukturierte/quasi-strukturierte Systeme
- aber: **niemals nur blind durchreichen**, sondern immer in eigenen Quality- und Versioning-Layer überführen

## Ingestion- und Qualitätsarchitektur

### Pipeline

1. Source change detected (Webhook/Delta/Polling)
2. Change Event in PostgreSQL schreiben
3. Worker pickt Event (`FOR UPDATE SKIP LOCKED`)
4. Raw artifact in S3 speichern
5. Content-Hash berechnen und gegen PostgreSQL vergleichen
6. Normalisierung in kanonisches Modell
7. Parsing / Struktur-Extraktion
8. Attachment Expansion
9. Chunking (Recursive 512 Tokens, 50-100 Overlap)
10. Chunk-Hashes berechnen, gegen gespeicherte vergleichen
11. tsvector für alle Chunks erzeugen
12. Selektiv Embeddings erzeugen (nur geänderte Chunks, nur Hot-/Warm-Tier)
13. Quality Gates
14. Conflict Detection
15. Publish (Status-Update in PostgreSQL)
16. Audit + Metrics + Review Tasks

### Parsing und Chunking

Chunking-Strategie (evidenzbasiert):

- **Recursive Character Splitting bei 512 Tokens, 50-100 Token Overlap** als Baseline
  - Vectara NAACL 2025 Studie: übertrifft konsistent semantisches Chunking bei Document Retrieval, Evidence Retrieval und Answer Generation
- Tabellen separat behandeln (eigener `chunk_type = 'table'`)
- Anhänge als eigene Dokumente plus Parent-Relation
- Überschriften als bevorzugte Chunk-Grenzen
- Kommentare optional separat indexieren
- Header/Footer deduplizieren
- Sehr kleine (<50 Tokens) und sehr große (>1000 Tokens) Chunks markieren

Warum Unstructured hier sinnvoll ist:

- Element-Typen statt nur Rohtext
- deterministische Element-IDs
- Tabellen als HTML
- Connector-Metadaten
- Chunking-Strategien wie `by_title`

### Quality Gates beim Indexieren

Drei Ebenen:

**1. Technische Qualität**

- Parser erfolgreich?
- Textmenge plausibel?
- OCR nötig oder fehlgeschlagen?
- Embeddings erzeugt (für Hot-Tier)?
- ACLs vorhanden?
- Anhänge geladen?
- Delta-Checkpoint konsistent?

**2. Strukturelle Qualität**

- Titel vorhanden?
- Tabelle korrekt extrahiert?
- Dubletten erkannt?
- Chunk-Größen innerhalb von Grenzen?
- Makros / Rich Content unvollständig?
- Zu hoher Boilerplate-Anteil?

**3. Semantische Qualität**

- Inhalt leer oder fast leer?
- Widersprüchlich zu höher priorisierten Quellen?
- Veraltet gegenüber neuerer Version?
- Metadaten und Inhalt inkonsistent?

### Ergebnis

Jeder Lauf erzeugt:

- `quality_score`
- `quality_findings[]`
- `publish_decision`

Mögliche Zustände:

- `published` (auto)
- `published_with_warnings`
- `quarantined`
- `needs_review`

## Retrieval-Architektur

### Stage 1: Hybrid Candidate Retrieval in PostgreSQL

Der größte Einzelhebel für Retrieval-Qualität: Hybrid Search (Dense + BM25) hebt Precision von ~62% auf ~84%.

```sql
-- Hybrid Search mit Reciprocal Rank Fusion in einer SQL-Query
WITH semantic AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1) AS rn
    FROM chunks
    WHERE tenant_id = $2
      AND embedding IS NOT NULL
    ORDER BY embedding <=> $1
    LIMIT 50
),
lexical AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY ts_rank(tsv, $3) DESC) AS rn
    FROM chunks
    WHERE tenant_id = $2
      AND tsv @@ $3
    ORDER BY ts_rank(tsv, $3) DESC
    LIMIT 50
)
SELECT COALESCE(s.id, l.id) AS id,
    COALESCE(1.0/(60+s.rn), 0) + COALESCE(1.0/(60+l.rn), 0) AS score
FROM semantic s
FULL OUTER JOIN lexical l ON s.id = l.id
ORDER BY score DESC
LIMIT 20;
```

Warum Hybrid hier Pflicht ist:

- BM25 findet exakte Codes ("E-4021"), Ticket-Keys ("SERV-2847"), Dateinamen
- Dense findet semantisch ähnliche Beschreibungen
- ACL- und Metadatenfilter greifen in derselben Query

### Stage 2: Cross-Encoder Reranking

Zweitgrößter Hebel: +20-35% Accuracy.

- Top-20 aus Hybrid Search an Cross-Encoder
- Cohere Rerank 4 (Cloud) oder BGE-Reranker (On-Prem)
- Ergebnis: Top-5 bis Top-10 mit hoher Relevanz
- ~50-200ms zusätzliche Latenz

Zusätzliche heuristische Features für Score Blending:

- Dokumentautorität
- Aktualität (Freshness)
- Review-Freigabe vorhanden
- Query-Intent passt zu Dokumenttyp

### Stage 3: Evidence Assembly

Vor dem LLM:

- Deduplizieren
- Gleiche Versionen gruppieren
- Konflikte markieren
- Unterschiedliche Quellen diversifizieren
- Chunks mit zu geringer Qualität rausfiltern

### Stage 4: Confidence / Abstention Layer

Der Service liefert einen Confidence Block:

- retrieval_confidence
- source_coverage
- freshness_score
- authority_score
- conflict_score
- citation_count
- answerable_yes_no

Wenn `answerable=no`:

- Nur Evidenz liefern
- Rückfrage stellen
- Review empfehlen

### Reranking-Reifegrade

1. Hybrid Retrieval + Standard Cross-Encoder (Phase 1)
2. Heuristisches Score Blending (Phase 2)
3. Learning-to-Rank mit Klick-, Feedback- und Review-Daten (Phase 3)
4. Eigener domänenspezifischer Reranker (Phase 3+)

## Qualitätsmaximierung

### Die fünf größten Hebel, nach Impact sortiert

| # | Hebel | Geschätzter Impact | Phase |
|---|-------|--------------------|-------|
| 1 | **Hybrid Search** (Dense + BM25 + RRF) | +22pp Precision (62% → 84%) | 1 |
| 2 | **Cross-Encoder Reranking** | +20-35% Accuracy | 1 |
| 3 | **Embedding-Modell-Wahl** | Baseline-Qualität | 1 |
| 4 | **Chunking-Strategie** (80% der RAG-Fehler kommen von hier) | +15-30% vs naive | 1 |
| 5 | **Query Transformation** (Multi-Query, HyDE) | +5-15% auf ambige Queries | 2 |

### Evaluation: Ohne Messung keine Qualität

**Metriken (RAGAS Framework):**

| Metrik | Ziel |
|--------|------|
| Recall@10 | >= 0.85 |
| Precision@5 | >= 0.70 |
| Faithfulness | >= 0.85 |
| Answer Relevancy | >= 0.80 |
| Context Recall | >= 0.80 |

**Golden Dataset:**

1. 50-100 manuell kuratierte Frage-Antwort-Kontext-Triples aus eurem Domänenwissen
2. Synthetische Daten via RAGAS zur Coverage-Erweiterung
3. Kontinuierliche Anreicherung mit echten Production-Queries
4. Strikte Versionierung für Vergleichbarkeit

**Wichtig:** MTEB-Benchmark-Scores auf öffentlichen Datasets übersetzen sich nicht 1:1 auf euren Corpus. Immer eigene Evaluation auf einer Stichprobe eurer Daten.

## Versionierung sichtbar machen

### Was versioniert werden sollte

- Rohdokument (in S3)
- normalisierte Dokumentrepräsentation
- Parser-/Extraktionsstand
- Chunk-Stand (mit Content-Hash)
- Embeddings (mit `embedding_model`)
- ACL-Zustand

### Was im Retrieval sichtbar sein sollte

Bei jedem Treffer:

- Dokumenttitel
- Quelle
- Dokumentversion
- letztes Änderungsdatum
- Indexierungszeit
- Review-Status
- ggf. "superseded by version X"

### Was in der UI / API möglich sein sollte

- Versionen auflisten
- Zwei Versionen vergleichen
- Unterschiede markieren
- Veraltete Inhalte kennzeichnen
- Antworten mit exakter Dokumentversion referenzieren

## Widersprüche und Konflikte

Das ist eines der wichtigsten Differenzierungsmerkmale.

### Vorschlag: Claim Graph (Phase 3)

Beim Indexieren werden aus hochwertigen Dokumentsegmenten Claims extrahiert:

- Subjekt, Prädikat, Objekt/Wert
- Einheit, Gültigkeitszeitraum
- Quelle, Evidenzspanne, Confidence

Beispiele:

- "Reiserichtlinie erlaubt Bahn 1. Klasse ab Management-Level X"
- "Support-Ticket-Priorität P1 Reaktionszeit = 30 Minuten"

### Konfliktarten

- **Hard conflict:** direkter Widerspruch
- **Soft conflict:** unterschiedliche Zahlen/Werte ohne klaren Kontext
- **Supersession:** neueres Dokument ersetzt älteres
- **Authority conflict:** zwei Quellen widersprechen, aber eine ist autoritativer
- **Duplication mismatch:** fast gleiche Dokumente mit abweichenden Kernaussagen

### Zwei Verwendungen

- **Index-time governance:** Reviewfälle erzeugen
- **Query-time transparency:** "Es existieren widersprüchliche Quellen zu diesem Thema"

## Empfohlene Technologiebausteine

### Baseline-Stack

| Schicht | Technologie | Begründung |
|---------|-------------|------------|
| **Service** | Python + FastAPI | Bestes Ökosystem für Parsing, Embeddings, NLI, Evaluation |
| **Orchestrierung** | Temporal | Durable Workflows, Idempotenz, Retries, Resume |
| **Datenbank** | PostgreSQL + pgvector + tsvector | Einzige DB: System of Record, Vektorspeicher, Full-Text, Change Detection |
| **Objekt-Storage** | MinIO/S3 | Rohdokumente, Anhänge, Parser-Artefakte |
| **Parsing** | Unstructured | Element-Typen, Tabellen, deterministische IDs |
| **Observability** | OpenTelemetry | Tracing, Metrics |
| **Evaluation** | RAGAS | Retrieval-Qualitätsmetriken |
| **Review** | Eigene Review UI | Für Kernfälle; optional Label Studio/Argilla |

### Warum keine separate Vector DB

- PostgreSQL + pgvector reicht für <10M Vektoren (typisch für Enterprise-Dokumente)
- Hybrid Search (tsvector + pgvector) in einer SQL-Query ist eine echte Stärke
- Transaktionale Konsistenz: Vektoren + Metadaten + ACLs in einer Transaktion
- On-Prem wird trivial einfacher (eine DB statt zwei)
- Skalierungspfad: pgvectorscale bei Bedarf (471 QPS bei 99% Recall auf 50M Vektoren)

### Wann doch eine separate Vector DB nachrüsten

- >10M Vektoren mit hohen QPS-Anforderungen
- Hoher concurrent Write-Durchsatz (>100 rows/sec sustained)
- Bedarf an starker Quantization (>2x Kompression)
- ColBERT/Multi-Vector nativ benötigt

In dem Fall: Qdrant als optionaler Retrieval-Accelerator. Das Tiered-Modell (PostgreSQL als Wahrheit) macht den Wechsel sauber.

### Temporal: Ab Phase 1 oder Phase 2?

Temporal ist die richtige Wahl für durable Workflows, aber eine schwere operationale Dependency.

**Phase 1 Alternative:** PostgreSQL-basierte Job-Queue mit `FOR UPDATE SKIP LOCKED`. Reicht für den MVP.

**Phase 2:** Temporal einführen wenn HA, komplexe Retries und Resume über Service-Neustarts hinweg nötig werden.

## Konkrete Produktbausteine

### Core APIs

#### Retrieval API

- `POST /v1/retrieve` -- Hybrid Search + Reranking
- `POST /v1/retrieve/explain` -- mit Ranking-Details

Rückgabe: Treffer, Zitate, Versionen, Confidence Block, Konflikthinweise

#### Structured Query API

- `POST /v1/query` -- Query Router entscheidet: RAG oder Structured
- `GET /v1/structured-sources` -- verfügbare Datenquellen
- `GET /v1/structured-sources/{id}/tools` -- verfügbare Tools

#### Document API

- `GET /v1/documents/{document_id}`
- `GET /v1/documents/{document_id}/versions`
- `GET /v1/document-versions/{version_id}`
- `GET /v1/chunks/{chunk_id}`

#### Governance API

- `GET /v1/conflicts`
- `POST /v1/conflicts/{id}/resolve`
- `POST /v1/reviews`
- `POST /v1/corrections`

#### Connector API

- `POST /v1/sources`
- `POST /v1/sources/{id}/sync`
- `GET /v1/sources/{id}/runs`
- `GET /v1/sources/{id}/health`

### Review-Workflow

Statusmodell:

- `new` → `auto_published` | `needs_review`
- `needs_review` → `approved` | `corrected` | `rejected`
- `approved` → `superseded`

Reviewfälle entstehen bei:

- Schlechter Parserqualität
- Fehlenden Anhängen
- Unklarer ACL
- Konflikterkennung
- Starker Dublettenabweichung
- Niedriger Confidence bei häufig genutzten Dokumenten

## Multi-Tenancy und Deployment

### Zentrales Hosting

- **Shared Control Plane** (API, Scheduler, Worker-Logik)
- **Customer-specific Data Plane** (PostgreSQL Schema/Database, S3 Bucket/Prefix)

### On-Prem

Durch den Verzicht auf eine separate Vector DB sind die Deployment-Profile deutlich einfacher:

#### 1. Evaluation

- Single Node: PostgreSQL + MinIO + API
- Lokales Embedding-Modell (Jina/BGE-M3 via TEI)
- Kleine Datenmenge, reduzierte SLOs

#### 2. Production Standard

- PostgreSQL HA (Primary + Replica)
- MinIO/S3
- API + Worker Pods
- Temporal (ab Phase 2)
- Cloud oder lokale Embedding-Modelle

#### 3. Air-Gapped / Regulated

- Lokale Embedding-/Rerank-Modelle (Qwen3-Embedding, BGE-Reranker)
- Kein externer SaaS-Zugriff
- Pull-only Connectoren oder kundenseitige Webhook-Relays
- RAGAS Evaluation mit lokalem LLM-Judge

### Tenant-Isolation

- Zentral/Shared: Schema-basierte Partitionierung plus strikte ACL/tenant Filter
- Reguliert/Dedicated: Separate PostgreSQL-Databases oder Deployments

## Security und Compliance

Pflichtfunktionen:

- Chunk-level ACL enforcement (in der SQL WHERE-Clause, VOR Reranking)
- Auditierbare Zugriffe (retrieval_logs)
- Verschlüsselte Artefakte in S3
- Secret Management für Connector-Credentials
- Vollständige Traces pro Sync-Run
- Nachvollziehbarkeit: "welcher Bot bekam welche Chunks aus welcher Version"
- Structured Query Audit: "welcher Tool-Call wurde mit welchen Parametern ausgeführt"

Wichtig:

ACLs müssen **vor** dem Re-Ranking und **vor** der Generierung greifen, nicht erst in der UI.

## KPIs und SLOs

### Betriebs-KPIs

- Sync success rate
- Sync lag P50/P95
- Parsing success rate
- Review backlog
- Conflict resolution time
- Tier distribution (% Hot/Warm/Cold)

### Retrieval-KPIs

- Recall@10 (Ziel: >= 0.85)
- Precision@5 (Ziel: >= 0.70)
- Faithfulness (Ziel: >= 0.85)
- Answerability precision
- Abstention quality
- Reranker uplift
- Hybrid vs. pure-vector delta

### Beispiel-SLOs

- P95 delta sync lag < 10 Minuten bei webhook-fähigen Quellen
- Parsing success > 99%
- ACL propagation errors = 0 tolerated
- Reviewpflichtige Dokumente innerhalb von 24h sichtbar
- Kritische Retrieval-API P95 < 500ms (Hybrid Search + Reranking, ohne LLM)
- Structured Query API P95 < 2s

## Roadmap

### Phase 0: Product Discovery und Architekturentscheidungen

Dauer: 2 bis 4 Wochen

Ziele:

- Top-Use-Cases definieren (Wissensfragen vs. Strukturierte Abfragen)
- Zielkundenprofile definieren
- Sicherheits- und On-Prem-Anforderungen klären
- Quellsystem-Priorisierung festlegen
- Datenmodell v1 festziehen (SQL-Schema)
- Embedding-Modell evaluieren (Deutsch-Performance)

Deliverables:

- Architektur-ADR
- SQL-Schema v1
- Connector-Verträge
- SLO-Entwurf
- Embedding-Modell-Benchmark auf eigenem Testcorpus

### Phase 1: MVP

Dauer: 6 bis 10 Wochen

Scope:

- **SharePoint** als erster Connector (meiste Edge Cases)
- PostgreSQL + pgvector + tsvector + MinIO
- Content-Hash-basierte Change Detection
- Tiered Indexing (Hot/Warm/Cold)
- Hybrid Search (Dense + BM25 + RRF) in PostgreSQL
- Cross-Encoder Reranking (Cohere Rerank oder BGE)
- REST API mit Retrieval + Document + Governance Endpoints
- Query Router v1 (RAG vs. Structured, vordefinierte Tools)
- Einfache Review UI
- Versionen sichtbar
- Embedding Provider Interface (ein Cloud + ein lokaler Provider)
- 50 Golden Query-Answer Pairs + RAGAS Evaluation

Nicht im MVP:

- Jira und Confluence Connectoren (Phase 1.5)
- MCP Adapter
- Eigener trainierter Reranker
- Komplexe Konfliktgraphen / Claim Graph
- Text-to-SQL
- Temporal (stattdessen PostgreSQL Job-Queue)
- Viele Long-Tail-Connectoren

### Phase 1.5: Weitere Connectoren

Dauer: 4 bis 6 Wochen

Scope:

- Jira Connector
- Confluence Connector
- MCP Adapter als dünne Fassade
- Query Router v1.5 mit mehr Tools

### Phase 2: Enterprise Hardening

Dauer: 8 bis 12 Wochen

Scope:

- Temporal für durable Workflows
- Dediziertes Deployment-Modell
- HA-Setups
- Quality Gates v2
- Konflikterkennung v1
- Review SLA Dashboard
- Bessere Observability
- Text-to-SQL als Option für Structured Queries
- Goldens + Offline Evaluation erweitern
- Tier-Promotion automatisieren
- Embedding-Modell-Migration-Tooling

### Phase 3: Differenzierung

Dauer: laufend

Scope:

- Claim Graph und automatische Konfliktcluster
- Domänenspezifisches LTR / eigener Reranker
- ColBERT Late Interaction (ggf. mit separater Vector DB)
- Feedback-basierte Retrieval-Optimierung
- Adaptive Query Transformation (HyDE, Multi-Query)
- Mehr Connectoren (Connector SDK + Airbyte/Unstructured)
- pgvectorscale bei Skalierungsbedarf

## Klare Empfehlungen

### 1. PostgreSQL als einzige Datenbank

pgvector + tsvector liefern Hybrid Search, Change Detection und System of Record in einem System. Keine separate Vector DB in Phase 1+2.

### 2. Nicht alles embedden -- Tiered Indexing

Hot/Warm/Cold Tiers. Nur 10-20% des Corpus braucht volle Vektoren. Der Rest lebt mit BM25 und on-demand Embedding.

### 3. Dualer Query-Pfad von Anfang an

Query Router trennt Wissensfragen (RAG) von Faktenfragen (Structured Query). Beide Use Cases in einem System.

### 4. Kontrolliert die Tier-1-Connectoren selbst

SharePoint, Jira und Confluence sind zu wichtig für externe Sync-Logik. SharePoint zuerst (meiste Edge Cases).

### 5. Hybrid Search + Reranking sind die größten Qualitätshebel

Hybrid Search hebt Precision von 62% auf 84%. Cross-Encoder Reranking bringt nochmal 20-35%. Beide ab Phase 1.

### 6. Cloud- und lokal-fähig ab Tag 1

Provider-Interface für Embedding und Reranking. Klein (zwei Interfaces, vier Implementierungen), aber entscheidend für On-Prem.

### 7. Führt Quality Gates schon vor dem ersten Kunden ein

Sonst wird aus Retrieval schnell unsichtbare Datenverschmutzung.

### 8. Messt Qualität von Anfang an

50 Golden Pairs + RAGAS. Ohne Messung keine Qualitätsverbesserung.

## Konkreter Startvorschlag

```
PostgreSQL (pgvector + tsvector)
    + MinIO/S3
    + FastAPI
    + SharePoint Connector (nativ)
    + Unstructured (Parsing)
    + Cohere Embed v4 / Qwen3-Embedding (Provider Interface)
    + Cohere Rerank 4 / BGE-Reranker (Provider Interface)
    + Hybrid Search (Dense + BM25 + RRF in SQL)
    + Content-Hash Change Detection
    + Tiered Indexing (Hot/Warm/Cold)
    + Query Router (RAG + Structured Tools)
    + Review UI
    + RAGAS Evaluation
```

Das ist minimal genug für eine einzelne Datenbank-Dependency, und stark genug für belastbares Enterprise-Retrieval mit dualem Query-Pfad.

## Quellen und Referenzen

### Spezifikationen und APIs

- MCP Resources: <https://modelcontextprotocol.io/specification/2025-06-18/server/resources>
- MCP Tools: <https://modelcontextprotocol.io/specification/2025-06-18/server/tools>
- MCP Transports: <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>
- Microsoft Graph driveItem delta: <https://learn.microsoft.com/en-us/graph/api/driveitem-delta?view=graph-rest-1.0>
- Microsoft Graph webhooks: <https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks>
- Microsoft Graph scan guidance: <https://learn.microsoft.com/en-us/onedrive/developer/rest-api/concepts/scan-guidance>
- Jira issue search: <https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/>
- Jira webhooks: <https://developer.atlassian.com/cloud/jira/platform/webhooks/>
- Confluence search: <https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-search/>
- Confluence content versions: <https://developer.atlassian.com/cloud/confluence/rest/v1/api-group-content-versions/>

### Technologie

- pgvector: <https://github.com/pgvector/pgvector>
- pgvectorscale: <https://github.com/timescale/pgvectorscale>
- pgvector 0.8 iterative scan: <https://aws.amazon.com/blogs/database/supercharging-vector-search-performance-and-relevance-with-pgvector-0-8-0-on-amazon-aurora-postgresql/>
- pgvector 150x speedup: <https://jkatz05.com/post/postgres/pgvector-performance-150x-speedup/>
- pgvector hybrid search: <https://jkatz05.com/post/postgres/hybrid-search-postgres-pgvector/>
- pgvectorscale vs Qdrant benchmark: <https://medium.com/timescale/pgvector-vs-qdrant-open-source-vector-database-comparison-f40e59825ae5>
- Temporal docs: <https://docs.temporal.io/>
- Unstructured connector docs: <https://docs.unstructured.io/open-source/ingestion/source-connectors/confluence>
- Unstructured document elements: <https://docs.unstructured.io/open-source/concepts/document-elements>

### Embedding und Retrieval

- Cohere Embed v4: <https://cohere.com/blog/embed-4>
- Cohere Rerank 4: <https://orq.ai/blog/from-noise-to-signal-how-cohere-rerank-4-improves-rag>
- Jina Embeddings v2 German: <https://jina.ai/news/ich-bin-ein-berliner-german-english-bilingual-embeddings-with-8k-token-length/>
- BGE-M3: <https://huggingface.co/BAAI/bge-m3>
- Qwen3-Embedding: <https://huggingface.co/Qwen/Qwen3-Embedding-8B>
- Anthropic Contextual Retrieval: <https://www.anthropic.com/news/contextual-retrieval>
- Jina Late Chunking: <https://jina.ai/news/late-chunking-in-long-context-embedding-models/>
- German ColBERT: <https://arxiv.org/html/2504.20083>

### Evaluation und Qualität

- RAGAS: <https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/>
- Vectara Chunking Benchmark (NAACL 2025): <https://blog.premai.io/rag-chunking-strategies-the-2026-benchmark-guide/>
- MTEB Benchmark: <https://huggingface.co/spaces/mteb/leaderboard>

### Architektur-Referenzen

- Milvus Tiered Storage: <https://milvus.io/blog/milvus-tiered-storage-80-less-vector-search-cost-with-on-demand-hot-cold-data-loading.md>
- Harvey AI Enterprise RAG: <https://www.harvey.ai/blog/enterprise-grade-rag-systems>
- Microsoft GraphRAG: <https://microsoft.github.io/graphrag/>
- RAPTOR: <https://arxiv.org/abs/2401.18059>
- Instacart pgvector Migration: <https://www.confident-ai.com/blog/why-we-replaced-pinecone-with-pgvector>
- Airbyte docs: <https://docs.airbyte.com/>
- Label Studio: <https://docs.humansignal.com/guide/quality.html>

## Developer Adoption Strategy

### Kernprinzip

Das Produkt hat zwei Zielgruppen:

1. **Enterprise-Kunden** — kaufen den managed Service oder ein Dedicated Deployment
2. **Entwickler** — integrieren RAASOA in eigene Produkte, bauen darauf auf, werden zu Multiplikatoren

Gruppe 2 ist der Wachstumshebel. Wenn Entwickler RAASOA schnell einsetzen können, landet es in immer mehr Produkten — und diese Produkte werden zu Enterprise-Kunden.

### Designprinzipien für Developer Experience

1. **5-Minuten-Quickstart**: `docker compose up` → API ready → curl-Beispiel funktioniert. Kein manuelles Setup von Embedding-Modellen oder Datenbanken.

2. **Drei Integrationstiefen**:
   - **HTTP API** — Jede Sprache, jedes Framework. curl reicht.
   - **Python Client SDK** — `pip install raasoa-client` → 3 Zeilen Code → fertig.
   - **MCP Server** — Claude, Cursor und andere AI-Agenten können direkt zugreifen.

3. **Progressive Disclosure**: Einfach starten, bei Bedarf tiefer konfigurieren. Defaults die funktionieren, ohne alles verstehen zu müssen.

4. **Beispiele statt Dokumentation**: `examples/quickstart.py`, `examples/fastapi_integration.py`, `examples/bulk_ingest.py` zeigen sofort, wie Integration aussieht.

### Artefakte für Developer Adoption

| Artefakt | Zweck | Status |
|----------|-------|--------|
| **README.md** (englisch) | Value Prop + Quickstart in 30 Sekunden | Vorhanden |
| **Docker Compose mit Ollama** | Zero-Dependency-Start | Vorhanden |
| **Python Client SDK** | `raasoa-client` Package | Vorhanden |
| **CLI Tool** | `raasoa ingest` / `raasoa search` | Vorhanden |
| **Beispiel-Skripte** | Quickstart, FastAPI Integration, Bulk Ingest | Vorhanden |
| **CONTRIBUTING.md** | Contributor-Onboarding | Vorhanden |
| **OpenAPI Spec** | Auto-generiert via FastAPI `/docs` | Vorhanden |
| **Docker Image auf GHCR** | `docker pull ghcr.io/raasoa/raasoa` | Phase 2 |
| **MCP Server Adapter** | Für Claude, Cursor, AI-Agenten | Phase 2 |
| **LangChain/LlamaIndex Integration** | Retriever-Plugin | Phase 2 |
| **TypeScript/JS Client** | Für Node.js/Frontend-Entwickler | Phase 3 |
| **Helm Chart** | Kubernetes-Deployment | Phase 3 |

### Open-Source-Strategie

- **Apache 2.0 Lizenz** — Maximale Freiheit für kommerzielle Nutzung
- **Core open, Enterprise features optional** — Der Retrieval-Service ist vollständig open source. Enterprise-Features (SSO, Advanced ACLs, SLA Dashboard) können als optionale Module oder managed Service angeboten werden.
- **Community zuerst** — Issues, PRs und Discussions auf GitHub. Keine Slack-Wall oder Anmeldeformulare.

*Letzte Aktualisierung: 2026-04-01*
