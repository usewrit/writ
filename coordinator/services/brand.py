"""
Product brand constants — single source of truth for the user-facing names of
Writ's own-brand AI and its distributed-crawl capability.

Firecrawl brands its execution service "fire-engine" and its agent model
"FIRE-1". Writ's equivalents:

- SCRIBE — Writ's own-brand AI: the autonomous concierge/agent AND the label
  the ai-gateway presents when it serves a Writ-hosted model (the "provider"
  a user picks alongside Claude / GPT). The agent "reads a site and writes it
  into a callable tool" — hence Scribe, which also rhymes with `writ`.
  ``SCRIBE_MODEL_ID`` ("scribe-1") is the versioned id, parallel to FIRE-1.

- DRAGNET — the coordinated, fleet-distributed crawl. One instruction casts a
  net across many agents, each handed part of the site, and everything caught
  lands in one deduped, queryable, change-tracked dataset.

Keep these here so a rename is one edit. Nothing below should hardcode the
strings — import from this module.
"""

# --- Scribe: Writ's own-brand AI (concierge agent + gateway model tier) ------
SCRIBE_NAME = "Scribe"
SCRIBE_MODEL_ID = "scribe-1"
SCRIBE_TAGLINE = "reads a site, writes it into a tool"

# --- Dragnet: the distributed coordinated crawl ------------------------------
DRAGNET_NAME = "Dragnet"
DRAGNET_TAGLINE = "map a whole site across the fleet, into one dataset"

# Workflow-type marker for the synthetic per-crawl workflow that shard results
# aggregate under (see crawl_orchestrator). Kept short — the column is VARCHAR(20).
CRAWL_WORKFLOW_TYPE = "crawl"

# Step type the agent special-cases as a distributed-crawl shard.
CRAWL_STEP_TYPE = "crawl_batch"

# AutomationTask.trigger_type stamped on every shard run. Crawl pages and workflow
# runs share the automation_tasks table, so this is what tells the two apart — a
# workflow's dataset must never serve shard rows, and vice versa.
CRAWL_TRIGGER_TYPE = "crawl"

# trigger_context keys carrying per-shard payload (kept underscored like the
# other private dispatch keys, e.g. _queued_form_data / _marketplace).
CTX_CRAWL_ID = "_crawl_id"
CTX_CRAWL_SHARD = "_crawl_shard"     # list[{"url","depth"}] handed to this shard
CTX_CRAWL_EXTRACT = "_crawl_extract"  # {"mode","schema","delay_ms"} extraction spec
