"""
Self-host coordinator settings — KV-backed operator knobs.

The single-owner OSS coordinator stores its operator-facing settings (Runtime
governor, Network / Public-URL, Security session policy, per-owner Preferences,
Data-retention) as JSON rows in the existing ``config`` key-value table (one row
per section). No new table is required.

Each section has:
  * a stable Config key,
  * a DEFAULTS dict (the built-in shape the UI renders when nothing is stored),
  * clamps/coercion so an operator can never persist a degenerate value.

``get_section``/``set_section`` are the generic helpers; the per-section
functions wrap them with the right key + coercion. Callers own the transaction
commit (``set_section`` flushes but does not commit) to match the rest of the
router code.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.config import Config

# ---------------------------------------------------------------------------
# Config keys (one JSON row per section)
# ---------------------------------------------------------------------------
KEY_RUNTIME = "coordinator_runtime"
KEY_NETWORK = "coordinator_network"
KEY_SECURITY = "coordinator_security"
KEY_PREFERENCES = "coordinator_preferences"
KEY_DATA = "coordinator_data"


# ---------------------------------------------------------------------------
# Defaults — mirror the desktop daemon's runtime shape / plan spec
# ---------------------------------------------------------------------------
RUNTIME_DEFAULTS: Dict[str, Any] = {
    "max_concurrent_runs": 4,
    "max_background_runs": 2,
    "rss_soft_watermark_mb": 1536,   # 0 = off
    "browser_headless": True,
    "min_content_check_interval_s": 60,
    "min_browser_check_interval_s": 300,
}

SECURITY_DEFAULTS: Dict[str, Any] = {
    "session_ttl_min": 15,        # access-token TTL (informational; module const today)
    "refresh_ttl_days": 30,
    "idle_timeout_min": 0,        # 0 = no idle timeout
    "require_mfa": False,
}

PREFERENCES_DEFAULTS: Dict[str, Any] = {
    # None = the owner has never picked a language, so the web UI keeps whatever
    # the browser resolved (navigator → en). Defaulting this to "en" would be
    # indistinguishable from an explicit English choice and would override a
    # French/Spanish browser on first load.
    "language": None,             # None | en | fr | es
    "theme": "system",            # light | dark | system
    # NO analytics/telemetry key here, deliberately. One used to sit here
    # (`analytics_enabled`, default False) with a Switch in the web UI promising it
    # "sends anonymized usage metrics" — and NOTHING in the coordinator ever read it.
    # A self-hosted build phones home nowhere (README: "no telemetry phoning home"),
    # so a toggle wired to nothing was a claim the code did not keep. `get_section`
    # surfaces only keys present here, so any value persisted by the old UI is already
    # invisible and is dropped from the row on the next write.
}

DATA_DEFAULTS: Dict[str, Any] = {
    "retention_days": 90,         # 0 = keep everything
}


# ---------------------------------------------------------------------------
# Generic KV helpers
# ---------------------------------------------------------------------------
async def get_section(db: AsyncSession, key: str, defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Return the stored section merged over its defaults (defaults fill gaps)."""
    row = await db.execute(select(Config).where(Config.key == key))
    cfg = row.scalar_one_or_none()
    stored = cfg.value if (cfg and isinstance(cfg.value, dict)) else {}
    merged = dict(defaults)
    for k in defaults:  # only surface known keys; ignore stray persisted junk
        if k in stored:
            merged[k] = stored[k]
    return merged


async def set_section(db: AsyncSession, key: str, values: Dict[str, Any]) -> None:
    """Upsert the section row. Caller commits."""
    row = await db.execute(select(Config).where(Config.key == key))
    cfg = row.scalar_one_or_none()
    if cfg:
        cfg.value = values
    else:
        db.add(Config(key=key, value=values))
    await db.flush()


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------
def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


SUPPORTED_LANGUAGES = ("en", "fr", "es")


def coerce_language(value: Any) -> Optional[str]:
    """Normalize a UI language to a supported code, or None for "not chosen".

    Unset/blank/unsupported values must stay None rather than collapsing to "en":
    the web UI only overrides the browser's own language when the owner has made an
    explicit choice, so "never picked" has to be distinguishable from "picked
    English". Region tags fold to their base (``fr-CA`` → ``fr``).
    """
    if value is None:
        return None
    base = str(value).split("-")[0].strip().lower()
    return base if base in SUPPORTED_LANGUAGES else None


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
async def get_runtime(db: AsyncSession) -> Dict[str, Any]:
    return await get_section(db, KEY_RUNTIME, RUNTIME_DEFAULTS)


async def set_runtime(db: AsyncSession, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge + clamp runtime governor knobs and persist. Returns the new section."""
    current = await get_runtime(db)
    for k, v in updates.items():
        if k not in RUNTIME_DEFAULTS:
            continue
        current[k] = v
    coerced = {
        "max_concurrent_runs": _int(current["max_concurrent_runs"], 4, 1, 64),
        "max_background_runs": _int(current["max_background_runs"], 2, 0, 64),
        "rss_soft_watermark_mb": _int(current["rss_soft_watermark_mb"], 1536, 0, 1_048_576),
        "browser_headless": _bool(current["browser_headless"], True),
        # Hard anti-detection floors from the daemon: content >= 30s, browser >= 60s.
        "min_content_check_interval_s": _int(current["min_content_check_interval_s"], 60, 30, 86_400),
        "min_browser_check_interval_s": _int(current["min_browser_check_interval_s"], 300, 60, 86_400),
    }
    await set_section(db, KEY_RUNTIME, coerced)
    return coerced


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
async def get_security(db: AsyncSession) -> Dict[str, Any]:
    return await get_section(db, KEY_SECURITY, SECURITY_DEFAULTS)


async def set_security(db: AsyncSession, updates: Dict[str, Any]) -> Dict[str, Any]:
    current = await get_security(db)
    for k, v in updates.items():
        if k not in SECURITY_DEFAULTS:
            continue
        current[k] = v
    coerced = {
        "session_ttl_min": _int(current["session_ttl_min"], 15, 1, 1_440),
        "refresh_ttl_days": _int(current["refresh_ttl_days"], 30, 1, 3_650),
        "idle_timeout_min": _int(current["idle_timeout_min"], 0, 0, 10_080),
        "require_mfa": _bool(current["require_mfa"], False),
    }
    await set_section(db, KEY_SECURITY, coerced)
    return coerced


# ---------------------------------------------------------------------------
# Preferences (per-owner)
# ---------------------------------------------------------------------------
async def get_preferences(db: AsyncSession) -> Dict[str, Any]:
    return await get_section(db, KEY_PREFERENCES, PREFERENCES_DEFAULTS)


async def set_preferences(db: AsyncSession, updates: Dict[str, Any]) -> Dict[str, Any]:
    current = await get_preferences(db)
    for k, v in updates.items():
        if k not in PREFERENCES_DEFAULTS:
            continue
        current[k] = v
    lang = coerce_language(current["language"])
    theme = str(current["theme"]).lower()
    if theme not in ("light", "dark", "system"):
        theme = "system"
    coerced = {
        "language": lang,
        "theme": theme,
    }
    await set_section(db, KEY_PREFERENCES, coerced)
    return coerced


# ---------------------------------------------------------------------------
# Data & retention
# ---------------------------------------------------------------------------
async def get_data(db: AsyncSession) -> Dict[str, Any]:
    current = await get_section(db, KEY_DATA, DATA_DEFAULTS)
    current["retention_days"] = _int(current["retention_days"], 90, 0, 3_650)
    return current


async def set_data(db: AsyncSession, updates: Dict[str, Any]) -> Dict[str, Any]:
    current = await get_data(db)
    for k, v in updates.items():
        if k not in DATA_DEFAULTS:
            continue
        current[k] = v
    coerced = {"retention_days": _int(current["retention_days"], 90, 0, 3_650)}
    await set_section(db, KEY_DATA, coerced)
    return coerced


# ---------------------------------------------------------------------------
# Network / Public URL
# ---------------------------------------------------------------------------
async def get_network(db: AsyncSession) -> Dict[str, Any]:
    """Return the persisted network overrides merged with env fallbacks.

    ``public_url`` prefers the persisted Config override, then the
    ``WRIT_PUBLIC_URL`` env (settings.writ_public_url). Trusted hosts default to
    the ``ALLOWED_HOSTS`` env when nothing is persisted.
    """
    import os
    from config import settings

    row = await db.execute(select(Config).where(Config.key == KEY_NETWORK))
    cfg = row.scalar_one_or_none()
    stored = cfg.value if (cfg and isinstance(cfg.value, dict)) else {}

    public_url = stored.get("public_url") or (settings.writ_public_url or "")
    public_url = (public_url or "").strip()

    trusted_hosts = stored.get("trusted_hosts")
    if not isinstance(trusted_hosts, list):
        env_hosts = (os.getenv("ALLOWED_HOSTS", "") or "").split(",")
        trusted_hosts = [h.strip() for h in env_hosts if h.strip()]

    from security import trusted_hosts as _trusted_hosts

    return {
        "public_url": public_url,
        "trusted_hosts": trusted_hosts,
        # The allowlist the middleware is enforcing right now — the operator's
        # entries plus the derived ones (this coordinator's own hostname,
        # loopback). Surfaced so the UI can explain a rejected Host without logs.
        "effective_trusted_hosts": _trusted_hosts.current(),
        "trusted_hosts_enforced": _trusted_hosts.is_enforced(),
        "derived": _derive_network_urls(public_url),
    }


def _derive_network_urls(public_url: str) -> Dict[str, Optional[str]]:
    """Read-only URLs the agents + API/MCP clients dial, derived from public_url."""
    base = (public_url or "").strip().rstrip("/")
    if not base:
        return {"agent_ws_url": None, "api_base": None, "mcp_base": None}

    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://"):]
    else:
        ws_base = base

    return {
        "agent_ws_url": f"{ws_base}/ws/ai-gateway",
        "api_base": f"{base}/api/v1",
        "mcp_base": f"{base}/api/mcp",
    }


async def apply_network_to_runtime(public_url: str, trusted_hosts: list) -> list:
    """Push a persisted network section into the LIVE process state.

    Both halves matter and both used to be half-done:

    * ``public_url`` goes onto the settings object so ``recorder_proxy`` hands
      agents the new gateway URL without a restart.
    * ``trusted_hosts`` goes into ``security.trusted_hosts``, which the Host-header
      middleware reads on every request. Before this call existed the field was
      persisted and rendered in Settings → Network and enforced by NOTHING —
      Starlette's TrustedHostMiddleware captured its allowlist once at startup.

    Returns the effective host allowlist (the operator's entries merged over the
    derived base, which always keeps loopback so the container healthcheck lives).
    """
    from config import settings
    from security import trusted_hosts as _trusted_hosts

    settings.writ_public_url = public_url or None
    return _trusted_hosts.apply(trusted_hosts)


async def restore_network_from_db(db: AsyncSession) -> None:
    """Re-apply the persisted network section at boot.

    Without this, a restart silently reverts to the env-only view: the operator's
    saved Public URL and Trusted hosts would be shown in the UI (``get_network``
    reads them back) while the running process enforced something else. Called
    from main's lifespan.
    """
    section = await get_network(db)
    await apply_network_to_runtime(section["public_url"], section["trusted_hosts"])


async def set_network(db: AsyncSession, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Persist public_url + trusted_hosts, and live-apply both.

    Neither value needs a restart: the public URL flows into the settings object
    that recorder_proxy reads, and the trusted hosts flow into the live Host-header
    allowlist."""
    current = await get_network(db)

    public_url = current["public_url"]
    trusted_hosts = current["trusted_hosts"]

    if "public_url" in updates:
        public_url = (str(updates["public_url"]) if updates["public_url"] is not None else "").strip()
    if "trusted_hosts" in updates and isinstance(updates["trusted_hosts"], list):
        trusted_hosts = [str(h).strip() for h in updates["trusted_hosts"] if str(h).strip()]

    to_store = {"public_url": public_url, "trusted_hosts": trusted_hosts}
    await set_section(db, KEY_NETWORK, to_store)

    # Live-apply BOTH halves — see apply_network_to_runtime. This is not
    # best-effort: if the host allowlist fails to update, the operator has just
    # been told their new hostname is trusted when it is not, so let the error
    # surface as a failed save instead of a silent lie.
    effective_hosts = await apply_network_to_runtime(public_url, trusted_hosts)

    return {
        "public_url": public_url,
        "trusted_hosts": trusted_hosts,
        # What the middleware will actually accept, including the derived
        # entries (the public URL's own hostname, loopback). The UI shows this
        # so "why is my alias still rejected?" is answerable without logs.
        "effective_trusted_hosts": effective_hosts,
        "derived": _derive_network_urls(public_url),
    }
