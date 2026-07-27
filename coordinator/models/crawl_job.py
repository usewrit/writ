"""
CrawlJob — the parent row that owns one distributed ("Dragnet") site crawl.

A crawl is NOT a single run: it maps a site and fans the pages out as many
shard tasks across the agent fleet (each an AutomationTask under a synthetic
per-crawl workflow, so their extracted_data aggregates through the normal
Workflow Data API + lineage dedup). This row holds the crawl's scope, politeness
budget, live counters, and terminal state. The live URL frontier / visited-set
lives in the coordinator's (in-process) Redis keyspace (see
services/crawl_orchestrator) — this table is the durable control-plane record and
the thing the UI + AI session poll.

Single-owner coordinator: there is no per-owner scoping column — the whole DB
belongs to the one operator.
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    Float,
    String,
    DateTime,
    Text,
    Boolean,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import relationship

from database import Base


# Terminal + live statuses. mapping = seeding the frontier (robots + sitemap);
# crawling = shards in flight; stopping = cancel requested, draining in-flight.
CRAWL_STATUSES = (
    "queued",
    "mapping",
    "crawling",
    "completed",
    "failed",
    "cancelled",
    "stopping",
)

# Terminal statuses (crawl has converged) and the live/active complement. Used by
# the trigger engine's crawl-dedup guard and by crawl-lifecycle event emission.
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
ACTIVE_STATUSES = tuple(s for s in CRAWL_STATUSES if s not in TERMINAL_STATUSES)


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String(200), nullable=False, comment="Human label, e.g. 'Dragnet: docs.example.com'")
    seed_url = Column(Text, nullable=False, comment="Entry URL the crawl starts from")

    # --- Scope ---------------------------------------------------------------
    include_paths = Column(JSON, nullable=True, default=list,
                           comment="Regex allowlist for URL paths (empty = all in-scope)")
    exclude_paths = Column(JSON, nullable=True, default=list,
                           comment="Regex denylist for URL paths")
    max_depth = Column(Integer, nullable=False, default=4,
                       comment="Max link-discovery depth; seed + sitemap pages are depth 0")
    same_domain = Column(Boolean, nullable=False, default=True,
                         comment="Only follow links on the seed's registrable domain")
    allow_subdomains = Column(Boolean, nullable=False, default=True,
                              comment="Follow links on subdomains of the seed domain")

    # --- Targeting (crawl "what the operator really wants") ------------------
    # A plain-English GOAL. When set, the coordinator derives include/exclude
    # paths + max_depth from it (services/crawl_targeting.derive_scope) and scores
    # the frontier by relevance so the page_budget flows to matching pages first.
    # Explicit include/exclude/max_depth still override the derived values.
    intent = Column(Text, nullable=True,
                    comment="Plain-English crawl goal; drives derived scope + frontier ranking")
    # Optional user-picked URL allowlist (from the /crawl/map picker). When set,
    # the frontier is seeded with exactly these (+ the seed) and the broad sitemap
    # expansion is skipped — the operator curated the set.
    seed_urls = Column(JSON, nullable=True,
                       comment="User-picked seed URLs from the map step (empty/null = auto-discover)")
    # Drop-below relevance score for the ranked frontier. 0.0 = disabled (keep
    # every in-scope URL, i.e. the plain breadth-first sweep).
    relevance_threshold = Column(Float, nullable=False, server_default="0", default=0.0,
                                 comment="Drop URLs scoring below this against the intent (0 = keep all)")
    # Provenance: what derive_scope returned for `intent`, for the detail page/audit.
    derived_scope = Column(JSON, nullable=True,
                           comment="Audit of the AI-derived scope {include_paths, exclude_paths, max_depth, reason}")

    # --- Extraction ----------------------------------------------------------
    # "markdown" = readability → clean markdown per page (Firecrawl-style).
    # "schema"   = replay a prebuilt fn_type:"list"/api extractor (extract_schema)
    #              on every matching page at ZERO marginal AI cost.
    extract_mode = Column(String(20), nullable=False, default="markdown",
                          comment="markdown | schema")
    extract_schema = Column(JSON, nullable=True,
                            comment="For schema mode: the extractor spec (row_selector + fields, or api replay)")
    # Content-selection spec (agents honor it in the markdown pipeline): which page ELEMENTS land in
    # the scrape. {preset: main|full|readable, include_comments, exclude_selectors, include_selectors,
    # keep:{images,tables,links}}. Null ⇒ default (main-content isolation, comments kept).
    content_spec = Column(JSON, nullable=True,
                          comment="Content-selection spec: preset + include/exclude CSS selectors + element toggles")

    # --- Render + document handling (orthogonal to output mode) --------------
    #   render_mode — auto (HTTP-first, browser fallback) | http (never render) |
    #                 browser (always warm-render: navigate → network-idle → settle).
    #   ocr_mode    — auto (text layer direct, OCR only pixels w/o text) | off | force.
    #                 Drives the doc-extract sidecar for PDF/office/image + scans.
    render_mode = Column(String(20), nullable=False, default="auto",
                         comment="auto | http | browser — page render strategy")
    ocr_mode = Column(String(20), nullable=False, default="auto",
                      comment="auto | off | force — OCR policy for non-HTML docs + scans")

    # --- Authenticated crawl (Writ differentiator) ---------------------------
    persona_id = Column(
        Integer,
        ForeignKey("personas.id", ondelete="SET NULL"),
        nullable=True,
        comment="Login identity used for auth'd crawls; restored per shard so pages behind a login are reachable",
    )

    # --- Politeness / budget -------------------------------------------------
    respect_robots = Column(Boolean, nullable=False, default=True,
                            comment="Honor robots.txt per discovered URL (crawl default is stricter than monitors)")
    delay_ms = Column(Integer, nullable=False, default=250,
                      comment="Politeness delay between page fetches inside a shard")
    max_concurrent_shards = Column(Integer, nullable=False, default=6,
                                   comment="Ceiling on shards in flight at once (global per-crawl throttle)")
    shard_size = Column(Integer, nullable=False, default=25,
                        comment="URLs handed to one agent per shard (one warm session)")
    page_budget = Column(Integer, nullable=False, default=1000,
                         comment="Hard ceiling on total pages fetched; stop cutting shards past it")

    # --- Aggregation ---------------------------------------------------------
    workflow_id = Column(
        Integer,
        ForeignKey("automation_workflows.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Synthetic per-crawl workflow shard results aggregate under (Workflow Data API)",
    )
    ai_session_id = Column(
        Integer,
        nullable=True,
        comment="AI session that launched this crawl (Scribe), if any",
    )

    # --- Lifecycle + live counters ------------------------------------------
    status = Column(String(20), nullable=False, default="queued", index=True)
    pages_discovered = Column(Integer, nullable=False, default=0)
    pages_done = Column(Integer, nullable=False, default=0)
    pages_failed = Column(Integer, nullable=False, default=0)
    pages_skipped = Column(Integer, nullable=False, default=0,
                           comment="URLs admitted then dropped (robots/scope/dupe at fetch time)")
    shards_dispatched = Column(Integer, nullable=False, default=0)
    shards_done = Column(Integer, nullable=False, default=0)
    current_depth = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)

    # --- Reconciliation (end-of-crawl) --------------------------------------
    # At finalize the coordinator dedups every shard's records by URL into ONE
    # canonical dataset count (shards otherwise each own a slice → "separated by
    # parts"). These are the authoritative totals the UI/API report; the deduped
    # rows themselves stay readable via the Workflow Data API lineage engine.
    records_total = Column(Integer, nullable=True,
                           comment="Unique records after end-of-crawl reconciliation (dedup by URL)")
    duplicates_removed = Column(Integer, nullable=True,
                                comment="Duplicate records collapsed during reconciliation")
    reconciled_at = Column(DateTime(timezone=True), nullable=True,
                           comment="When the end-of-crawl reconciliation pass ran")

    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=datetime.utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    workflow = relationship("AutomationWorkflow", foreign_keys=[workflow_id], lazy="noload")

    # NOTE: no __table_args__ index on `status` — the column already declares
    # index=True, which emits `ix_crawl_jobs_status`. Declaring it a second time
    # here made metadata-driven create_all fail with "index already exists".

    def __repr__(self) -> str:
        return (
            f"<CrawlJob(id={self.id}, seed='{self.seed_url}', "
            f"status='{self.status}', done={self.pages_done}/{self.pages_discovered})>"
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def summary(self) -> dict:
        """Serializable status snapshot for the API / AI session progress."""
        return {
            "id": self.id,
            "name": self.name,
            "seed_url": self.seed_url,
            # Site favicon proxy (a crawl is one site) — same-origin, resolved +
            # cached on first load, 404→globe glyph. See routers/crawl favicon.
            "favicon": f"/crawl/{self.id}/favicon",
            "status": self.status,
            "extract_mode": self.extract_mode,
            "intent": self.intent,
            "derived_scope": self.derived_scope,
            "relevance_threshold": self.relevance_threshold,
            "render_mode": self.render_mode,
            "ocr_mode": self.ocr_mode,
            "workflow_id": self.workflow_id,
            "pages_discovered": self.pages_discovered,
            "pages_done": self.pages_done,
            "pages_failed": self.pages_failed,
            "pages_skipped": self.pages_skipped,
            "shards_dispatched": self.shards_dispatched,
            "shards_done": self.shards_done,
            "current_depth": self.current_depth,
            "max_depth": self.max_depth,
            "page_budget": self.page_budget,
            "records_total": self.records_total,
            "duplicates_removed": self.duplicates_removed,
            "reconciled_at": self.reconciled_at.isoformat() if self.reconciled_at else None,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }
