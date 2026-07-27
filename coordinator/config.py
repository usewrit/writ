"""
Configuration management for Writ coordinator.
Loads settings from environment variables with sensible defaults.
"""
import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, RedisDsn, model_validator


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    # Database (self-host: local SQLite, not Postgres).
    #
    # The self-hosted coordinator ships a single-file SQLite database — it
    # dispatches all browser work to fleet agents, so it has no need for a
    # networked Postgres. WRIT_DB_PATH points at the .db file (default
    # ./writ.db locally, /data/writ.db in the container); the async
    # `database_url` property below derives the SQLAlchemy URL from it so every
    # existing `str(settings.database_url)` call site keeps working unchanged.
    writ_db_path: str = Field(
        default="./writ.db",
        description=(
            "Filesystem path to the coordinator's SQLite database file. "
            "env WRIT_DB_PATH (default ./writ.db; use /data/writ.db in a container)."
        ),
    )

    # Redis
    redis_url: RedisDsn = Field(
        default="redis://localhost:6379/0",
        description="Redis connection string"
    )

    # Security
    api_secret_key: str = Field(
        default="change-this-in-production-use-openssl-rand-hex-32",
        description="Secret key for API token generation"
    )
    hmac_secret_key: str = Field(
        default="change-this-in-production-use-openssl-rand-hex-32",
        description="Secret key for HMAC signature verification"
    )
    secret_encryption_key: Optional[str] = Field(
        default=None,
        description="Fernet key for encrypting agent secrets (generate with: python -m cryptography.fernet)"
    )
    relay_inbound_secret: Optional[str] = Field(
        default=None,
        description="Shared secret for HMAC-signing inbound OTP relay messages (email/SMS forwarders POST to /api/relay/inbound). Falls back to hmac_secret_key if unset."
    )

    # JWT
    jwt_secret_key: Optional[str] = Field(
        default=None,
        description="Secret key for JWT tokens (falls back to api_secret_key if not set)"
    )

    # Public URL of this self-hosted coordinator. Handed back to agents as the
    # gateway_ws_url base by recorder_proxy, and used to build absolute links in
    # emails / OAuth metadata. A single hostname serves both frontend and API.
    writ_public_url: Optional[str] = Field(
        default=None,
        description="Public base URL of this coordinator (e.g. https://writ.example.com). Used for agent gateway URLs and absolute links."
    )

    # ── Document / OCR extraction ────────────────────────────────────────────
    # The address of the doc-extract service, and the secret callers present to
    # it. The coordinator NEVER calls doc-extract itself — agents do, with bytes
    # they already fetched. These exist so the coordinator can hand the settings
    # to an agent at connect time, which is what makes PDF/OCR coverage work out
    # of the box instead of being a thing every operator has to wire by hand.
    #
    # docker-compose.yml sets both. The default is the loopback address the
    # bundled service publishes, which is correct for an agent on this same
    # host; point it at a routable address if your agents run elsewhere.
    doc_extract_url: str = Field(
        default="http://127.0.0.1:8092",
        description=(
            "Base URL of the document/OCR extraction service, as reachable FROM "
            "YOUR AGENTS (not from the coordinator). Handed to agents at connect "
            "time. Blank disables the lane. env DOC_EXTRACT_URL."
        ),
    )
    doc_extract_secret: Optional[str] = Field(
        default=None,
        description=(
            "Shared secret agents present to the document/OCR extraction "
            "service. Must match that service's own DOC_EXTRACT_SECRET. "
            "env DOC_EXTRACT_SECRET."
        ),
    )

    # ── AGPL-3.0 §13: the network source offer ───────────────────────────────
    # This coordinator is AGPL-3.0-only, and §13 requires that anyone who
    # interacts with it *over a network* be offered the CORRESPONDING SOURCE of
    # the version they are talking to. The app satisfies that by serving
    # GET /api/about (public, unauthenticated) and linking it from the UI.
    #
    # If you MODIFY this coordinator and let anyone else use it over a network,
    # point writ_source_url at YOUR fork — the upstream default no longer is the
    # corresponding source of what you are running. That is exactly why this is
    # an env var and not a constant.
    writ_source_url: str = Field(
        default="https://github.com/usewrit/writ",
        description=(
            "URL where the complete corresponding source of THIS running "
            "coordinator can be obtained (AGPL-3.0 §13). Change it to your own "
            "repository if you deploy a modified build. env WRIT_SOURCE_URL."
        ),
    )

    # Legal consent — current document versions (bumping forces a re-accept; see
    # auth register / re-consent gate). Plain date strings (no DB migration here;
    # the user columns live in the legal_consent migration).
    terms_version: str = Field(
        default="2026-06-20",
        description=(
            "Current Terms of Service version. Stamped onto users at signup and "
            "compared on login — bumping this prompts existing users to re-accept."
        ),
    )
    privacy_version: str = Field(
        default="2026-06-20",
        description=(
            "Current Privacy Policy version. Stamped onto users at signup and "
            "compared on login — bumping this prompts existing users to re-accept."
        ),
    )

    # NOTE: CAPTCHA_PROVIDER / CAPTCHA_SECRET were removed. They documented
    # "pluggable bot defense enforced on register / login" backed by
    # services/captcha_service.py — a module that does not exist in this build,
    # with no consumer anywhere in the tree. Settings that advertise a security
    # control which is not actually enforced are worse than no settings at all:
    # an operator could set them, see no error, and believe registration was
    # protected. Brute-force throttling on the auth routes (see
    # security/brute_force.py, wired in routers/auth.py) is the control that IS
    # in force here.

    # Trusted upstream country header (sanctions gate). Read by
    # services/geoip_service.country_from_request to back the self-declared signup
    # country with an edge-resolved one. Operator: only trustworthy when
    # Cloudflare (or an equivalent edge that injects/overwrites this header) is in
    # front of the API; set to match the deployed edge (Cloudflare=CF-IPCountry,
    # CloudFront=CloudFront-Viewer-Country, ...).
    geoip_trusted_header: str = Field(
        default="CF-IPCountry",
        description=(
            "Trusted upstream country header (ISO-3166 alpha-2) set by the edge "
            "proxy (e.g. Cloudflare's CF-IPCountry). Used to back the self-declared "
            "signup country in the sanctions/embargo gate. Spoofable unless a "
            "trusted edge strips client-supplied copies and injects its own."
        ),
    )

    # Per-target-domain run rate limiting — caps how many runs may be
    # dispatched against one third-party host within a rolling window so the
    # browser fleet can't be used as a DDoS amplifier. 0 disables. Enforced at
    # the central dispatch choke point (services/target_rate_limit.py). FAIL-OPEN.
    target_domain_rate_limit: int = Field(
        default=30,
        description=(
            "Max runs the coordinator may dispatch against one target host within "
            "target_domain_rate_window_secs. 0 disables. Fail-open abuse control "
            "(not a money/authz gate) enforced in services/target_rate_limit.py."
        ),
    )
    target_domain_rate_window_secs: int = Field(
        default=60,
        description="Rolling window (seconds) for target_domain_rate_limit.",
    )

    # Data retention — purge windows (days) enforced by the retention loop. Each
    # bounds how long a class of records is kept before deletion. Match these to
    # the published privacy policy.
    run_retention_days: int = Field(
        default=90,
        description="Days to retain workflow/automation run records before purge.",
    )
    log_retention_days: int = Field(
        default=90,
        description="Days to retain audit/event log records before purge.",
    )
    detected_change_retention_days: int = Field(
        default=90,
        description="Days to retain detected-change records before purge.",
    )

    # Local-filesystem storage root for binary blobs (visual snapshots + stored
    # files). This is the DEFAULT storage backend for the shipped single-container
    # setup: docker/docker-compose.yml and Dockerfile.coordinator set
    # WRIT_FILES_DIR=/data/files (a persistent volume). services/visual_storage.py
    # selects backends in this order: explicit MinIO env config → MinIO;
    # else writ_files_dir set → local filesystem (visuals/ + uploads/ subtrees,
    # opaque UUID fan-out keys, atomic writes); else → base64 blobs inside the
    # SQLite rows (bloats writ.db — a one-time startup warning says how to fix).
    writ_files_dir: Optional[str] = Field(
        default=None,
        description=(
            "Directory where the coordinator stores binary objects (region "
            "screenshots, diff overlays, uploaded files) when MinIO/S3 is not "
            "configured. Unset (default) with no MinIO → binaries are stored as "
            "base64 inside SQLite. The shipped Docker setup sets /data/files. "
            "env WRIT_FILES_DIR."
        ),
    )

    # File assets (StoredFile) — object storage + per-file/quota policy. The bytes
    # live in a dedicated MinIO bucket (separate from visual-snapshots), served
    # only via short-TTL presigned GET / same-origin proxy (never leak MinIO
    # creds). Quota/size/type are enforced backend-authoritatively in file_service.
    minio_files_bucket: str = Field(
        default="tenant-files",
        description=(
            "MinIO bucket holding file assets (StoredFile bytes). Created on "
            "first use. env MINIO_FILES_BUCKET."
        ),
    )
    file_max_bytes: int = Field(
        default=100 * 1024 * 1024,
        description=(
            "Per-file hard size cap (bytes) enforced at every create path "
            "(upload/api/capture). Default 104857600 = 100MB. env FILE_MAX_BYTES."
        ),
    )
    file_signed_url_ttl_seconds: int = Field(
        default=600,
        description=(
            "TTL (seconds) for the short-lived, single-object presigned GET URLs "
            "used to serve / dispatch a stored file. Keep >= expected run duration. "
            "env FILE_SIGNED_URL_TTL_SECONDS."
        ),
    )
    file_allowed_content_types: list[str] = Field(
        default_factory=list,
        description=(
            "Optional content-type ALLOWLIST. Empty (default) = allow all except a "
            "conservative executable denylist (see file_service). When set, only "
            "these exact content types are accepted. env FILE_ALLOWED_CONTENT_TYPES."
        ),
    )
    file_ephemeral_ttl_seconds: int = Field(
        default=24 * 3600,
        description=(
            "Default TTL (seconds) applied to ephemeral files (source="
            "workflow_output / ai_session / streaming) unless promoted to the "
            "library. Default 86400 = 24h. env FILE_EPHEMERAL_TTL_SECONDS."
        ),
    )

    # --- Object-store backend selection for file assets (env-fallback layer) ---
    # Resolution order for where a file's bytes are written (see §2.7 /
    # storage_provider_service.resolve_provider):
    #   own StorageProvider -> default StorageProvider
    #     -> these env FILES_S3_* (an external S3-compatible provider furnished by
    #        env, no DB row) -> local MinIO.
    # AWS S3, DigitalOcean Spaces, Cloudflare R2 and MinIO all speak the same S3
    # API, so this is purely a client-construction switch — helper signatures and
    # call sites stay storage-agnostic (everything goes through
    # visual_storage._files_store). Leave files_s3_endpoint empty to fall straight
    # through to MinIO.
    files_s3_endpoint: str = Field(
        default="",
        description=(
            "External S3-compatible endpoint for file assets when no DB "
            "StorageProvider applies (e.g. https://nyc3.digitaloceanspaces.com, "
            "https://s3.amazonaws.com). Empty = use local MinIO. env FILES_S3_ENDPOINT."
        ),
    )
    files_s3_region: str = Field(
        default="us-east-1",
        description="Region for the env FILES_S3_* provider. env FILES_S3_REGION.",
    )
    files_s3_access_key: str = Field(
        default="",
        description="Access key for the env FILES_S3_* provider. env FILES_S3_ACCESS_KEY.",
    )
    files_s3_secret_key: str = Field(
        default="",
        description="Secret key for the env FILES_S3_* provider. env FILES_S3_SECRET_KEY.",
    )
    files_s3_bucket: str = Field(
        default="",
        description=(
            "Bucket for the env FILES_S3_* provider (falls back to "
            "minio_files_bucket when empty). env FILES_S3_BUCKET."
        ),
    )
    files_s3_public_base_url: str = Field(
        default="",
        description=(
            "Optional public/CDN base URL for object + presigned URLs of the env "
            "FILES_S3_* provider. env FILES_S3_PUBLIC_BASE_URL."
        ),
    )

    # SMTP (email)
    smtp_host: Optional[str] = Field(
        default=None,
        description="SMTP server hostname"
    )
    smtp_port: int = Field(
        default=587,
        description="SMTP server port"
    )
    smtp_username: Optional[str] = Field(
        default=None,
        description="SMTP username"
    )
    smtp_password: Optional[str] = Field(
        default=None,
        description="SMTP password"
    )
    smtp_from_email: Optional[str] = Field(
        default=None,
        description="From email address"
    )
    smtp_from_name: str = Field(
        default="Writ",
        description="From display name"
    )
    smtp_use_tls: bool = Field(
        default=True,
        description="Use TLS for SMTP"
    )

    # Scheduling
    global_period_ms: int = Field(
        default=1000,
        description="Global scheduling period in milliseconds",
        ge=100,
        le=3600000
    )
    quorum: int = Field(
        default=2,
        description="Number of agents required to confirm a change",
        ge=1,
        le=10
    )
    # Runtime governor: ceiling on how many scheduled workflow runs the scheduler
    # dispatches concurrently to the fleet (asyncio.Semaphore). A live override can
    # be stored in the Config KV under "max_concurrent_runs" (int); the scheduler
    # reads that first and falls back to this default.
    # Floor only: this setting IS the operator's own governor, so capping it would
    # just be the coordinator second-guessing the person who owns the fleet. Real
    # backpressure comes from live agent capacity + the RAM watermark.
    max_concurrent_runs: int = Field(
        default=5,
        description="Max scheduled workflow runs dispatched to the fleet at once.",
        ge=1,
    )

    # Notifications - Pushover
    pushover_app_token: Optional[str] = Field(
        default=None,
        description="Pushover application token"
    )
    pushover_user_key: Optional[str] = Field(
        default=None,
        description="Pushover user key"
    )

    # Notifications - Twilio
    twilio_account_sid: Optional[str] = Field(
        default=None,
        description="Twilio account SID"
    )
    twilio_auth_token: Optional[str] = Field(
        default=None,
        description="Twilio authentication token"
    )
    twilio_from_phone: Optional[str] = Field(
        default=None,
        description="Twilio source phone number"
    )
    twilio_to_phone: Optional[str] = Field(
        default=None,
        description="Twilio destination phone number"
    )

    # APNs
    apns_key_id: Optional[str] = Field(
        default=None,
        description="Apple Push Notification service key ID"
    )
    apns_team_id: Optional[str] = Field(
        default=None,
        description="Apple Developer team ID"
    )
    apns_bundle_id: Optional[str] = Field(
        default=None,
        description="iOS app bundle identifier (APNs push is a hosted-only feature; unused in self-host)"
    )
    apns_key_path: Optional[str] = Field(
        default=None,
        description="Path to APNs .p8 key file"
    )
    apns_use_sandbox: bool = Field(
        default=True,
        description="Use APNs sandbox environment"
    )

    # Social Auth (OAuth login)
    google_client_id: Optional[str] = Field(default=None, description="Google OAuth client ID")
    google_client_secret: Optional[str] = Field(default=None, description="Google OAuth client secret")
    github_client_id: Optional[str] = Field(default=None, description="GitHub OAuth client ID")
    github_client_secret: Optional[str] = Field(default=None, description="GitHub OAuth client secret")
    apple_client_id: Optional[str] = Field(default=None, description="Apple OAuth client ID (Services ID)")
    apple_client_secret: Optional[str] = Field(default=None, description="Apple OAuth client secret (JWT)")
    microsoft_client_id: Optional[str] = Field(default=None, description="Microsoft OAuth client ID (for Persona email-OTP mail connect)")
    microsoft_client_secret: Optional[str] = Field(default=None, description="Microsoft OAuth client secret")

    # OAuth
    oauth_access_token_expire_seconds: int = Field(
        default=3600,
        description="OAuth access token TTL in seconds (default: 1 hour)"
    )
    oauth_refresh_token_expire_days: int = Field(
        default=90,
        description="OAuth refresh token TTL in days (default: 90 days)"
    )
    oauth_device_refresh_token_expire_days: int = Field(
        default=3650,
        description=(
            "Refresh-token TTL (days) for the OAuth 2.0 Device Authorization Grant used by the "
            "Writ desktop app / CLI (the public 'writ-agent' client). Deliberately FAR longer than "
            "both the generic OAuth refresh TTL and the web session (~15m access / 7d refresh) so a "
            "linked desktop install stays signed in effectively indefinitely. The token rotates and "
            "this window SLIDES on every refresh, so any active install keeps re-extending it; only a "
            "fully idle (never-launched) install for the whole window, or a server-side revoke, logs "
            "it out. Default: 3650 days (~10 years)."
        )
    )
    oauth_authorization_code_expire_seconds: int = Field(
        default=600,
        description="OAuth authorization code TTL in seconds (default: 10 minutes)"
    )
    frontend_url: str = Field(
        default="http://localhost:3000",
        description="Frontend URL for OAuth consent redirects"
    )

    # WebAuthn / passkeys
    webauthn_rp_name: str = Field(
        default="Writ",
        description="Relying-Party display name shown in the OS passkey / biometric prompt",
    )
    webauthn_rp_id: str = Field(
        default="",
        description=(
            "WebAuthn Relying Party ID — the registrable domain passkeys are bound to "
            "(e.g. 'writ.com'). Empty = derive from frontend_url's hostname. Must be a "
            "suffix of the origin host; changing it invalidates existing passkeys."
        ),
    )

    # Refresh-token rotation
    refresh_rotation_grace_seconds: int = Field(
        default=30,
        description=(
            "How long a just-rotated refresh token stays redeemable, returning the "
            "successor it already minted. Absorbs the browser races that otherwise "
            "log the owner out (reload during an in-flight refresh, two tabs booting "
            "together). 0 = strict single-use. See security/jwt.py."
        ),
    )

    # CORS
    cors_origins: str = Field(
        # The single-container self-host serves the SPA and the API from the SAME
        # process on :8000 (docker/entrypoint.sh, docker-compose.yml), so that is
        # the origin an unconfigured install actually runs on — matching
        # .env.example. 3000/8080 kept for split dev servers.
        default="http://localhost:8000,http://localhost:3000,http://localhost:8080",
        description="Comma-separated list of allowed CORS origins"
    )

    # FX rates (DISPLAY-ONLY currency layer) — optional live source for USD-base
    # conversion rates shown next to USD prices in the UI. Money always settles in
    # USD; these rates never bill/settle. Expects an exchangerate.host / ECB-style
    # JSON body ({"rates": {CODE: number, ...}} keyed in USD). Unset => the service
    # serves static fallback rates (services/fx_service.SEED_RATES). Referenced via
    # getattr(settings, "fx_rates_url", None), so this is a soft/optional dependency.
    fx_rates_url: Optional[str] = Field(
        default=None,
        description=(
            "Optional URL for live USD-base FX rates (exchangerate.host / ECB-style "
            "JSON: {\"rates\": {CODE: number}}). Display-only — never used to bill or "
            "settle. Unset falls back to static seed rates."
        ),
    )

    # Server
    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, description="Server port", ge=1, le=65535)
    coordinator_url: str = Field(
        default="http://localhost:8000",
        description="URL for internal coordinator API calls (used by virtual agents)"
    )
    log_level: str = Field(default="info", description="Logging level")
    environment: str = Field(default="development", description="Environment name")

    # Explicit opt-in to boot with the shipped DEFAULT API/JWT signing secret.
    # Secure-by-default: the coordinator refuses to start on a shipped/well-known
    # signing secret in EVERY environment (see _validate_production_secrets). The
    # ONLY escape hatch is a throwaway local trial, which requires BOTH
    # ENVIRONMENT=development AND this flag set true; it is ignored in every other
    # environment. Never set this on a deployed/internet-reachable instance — the
    # HS256 session/API tokens would be signed with a public key and forgeable.
    allow_insecure_dev: bool = Field(
        default=False,
        description=(
            "Local-dev-only opt-in to run with the shipped default API/JWT signing "
            "secret. Honoured ONLY when ENVIRONMENT=development; ignored otherwise. "
            "env ALLOW_INSECURE_DEV."
        ),
    )

    # SSRF posture is deliberately DECOUPLED from `environment`: the private-IP
    # screen (loopback, RFC1918, link-local/metadata 169.254.169.254, reserved)
    # is ON by default in EVERY environment, including development. Set this
    # true ONLY when this coordinator must legitimately monitor/crawl intranet
    # apps (private-address targets); it disables the private/internal-range
    # portion of the SSRF screen everywhere it is consulted (safe_fetch,
    # url_policy, webhook delivery, target validation). All other SSRF screens
    # (scheme checks, DNS pinning, redirect re-validation) stay active.
    allow_private_targets: bool = Field(
        default=False,
        description=(
            "Allow monitored/crawled/webhook targets to resolve to private or "
            "internal IP ranges. Needed for monitoring intranet apps; leave "
            "false otherwise — SSRF screening stays on in every environment. "
            "env ALLOW_PRIVATE_TARGETS."
        ),
    )

    # Which upstream peers uvicorn trusts to set the X-Forwarded-For / -Proto
    # headers (proxy_headers). Read by serve.py. Default '127.0.0.1' trusts ONLY a
    # loopback reverse proxy, so a direct client cannot spoof its source IP and
    # defeat per-IP controls (rate-limit / ip-ban). Set this to the actual
    # address(es)/CIDR of your reverse proxy. Do NOT use '*' on an
    # internet-reachable deployment — it trusts every client's forwarded IP.
    forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        description=(
            "Comma-separated client IPs/CIDRs uvicorn trusts for X-Forwarded-For "
            "(proxy_headers). Default '127.0.0.1' (loopback proxy only). Set to your "
            "reverse-proxy address; never '*' in production. env FORWARDED_ALLOW_IPS."
        ),
    )

    # Rate Limiting
    rate_limit_requests: int = Field(
        default=100,
        description="Max requests per window",
        ge=1
    )
    rate_limit_window: int = Field(
        default=60,
        description="Rate limit window in seconds",
        ge=1
    )

    # Platform Controls (can be overridden at runtime via Redis)
    maintenance_mode: bool = Field(
        default=False,
        description="When True, all non-admin requests return 503 Service Unavailable"
    )
    registration_enabled: bool = Field(
        default=True,
        description="When False, new user registration is disabled"
    )
    login_enabled: bool = Field(
        default=True,
        description="When False, all login attempts are rejected (emergency lockdown)"
    )
    # Step-up requirement for platform owners. Default OFF so dev/tests and any
    # existing deployment are unchanged until an operator opts in (REQUIRE_ADMIN_MFA
    # =true) — flip it ON only AFTER every platform admin has enrolled a second
    # factor, or they'll lock themselves out of admin endpoints. Enforced live in
    # security.dependencies.require_platform_admin by re-checking the DB on every
    # request (a JWT can't carry live MFA state); enrollment + self/profile routes
    # are NOT behind that gate, so an un-enrolled admin can always reach enrollment.
    require_admin_mfa: bool = Field(
        default=False,
        description="When True, platform admins must have MFA (TOTP or a passkey) enabled to reach admin endpoints"
    )

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """Reject insecure secret values (fail-closed on shipped defaults)."""
        import logging

        logger = logging.getLogger(__name__)
        env = self.environment.lower()

        # Minimum accepted length for any signing secret. Every doc and helper in
        # the tree generates these with `openssl rand -hex 32` (64 chars), so this
        # floor rejects typos and truncated pastes without touching real configs.
        MIN_SECRET_LENGTH = 32

        def _is_weak_secret(value: Optional[str], *, allow_unset: bool = False) -> bool:
            """True when `value` must not be used to sign tokens.

            BLANK IS WEAK. An empty string is what you get from a `.env` template
            copied but not filled in (`JWT_SECRET_KEY=`), or from an orchestrator
            interpolating an unset variable. python-jose signs HS256 with a
            zero-length key perfectly happily, so a blank secret is not a
            misconfiguration that fails later — it is a publicly known signing key
            that lets anyone forge an is_platform_admin session. It must be caught
            here, at boot.

            `allow_unset=True` is for secrets that legitimately fall back to
            another one when absent (jwt_secret_key -> api_secret_key). It permits
            None (never set) but still rejects an explicitly blank value.
            """
            if value is None:
                return not allow_unset
            stripped = value.strip()
            if not stripped:
                return True
            if len(stripped) < MIN_SECRET_LENGTH:
                return True
            return (
                stripped.startswith("change-this")
                or stripped.startswith("dev_")
                or stripped == "__DEV_ONLY_REPLACE_ME__"
            )

        # Secure-by-default token-signing guard — applies in EVERY environment.
        # api_secret_key is the API-token secret AND the JWT signing fallback
        # (security/jwt.py: jwt_secret_key or api_secret_key). If either still
        # carries a shipped/well-known default, ANYONE can forge an is_platform_admin
        # session, so we refuse to boot regardless of `environment`. The ONE escape
        # hatch is an explicit throwaway local trial: ENVIRONMENT=development AND
        # ALLOW_INSECURE_DEV=true. Even then we emit a loud warning so it can never
        # pass silently.
        signing_is_default = _is_weak_secret(self.api_secret_key) or _is_weak_secret(
            self.jwt_secret_key, allow_unset=True
        )
        insecure_dev_optin = env == "development" and self.allow_insecure_dev
        if signing_is_default:
            if not insecure_dev_optin:
                raise ValueError(
                    "API_SECRET_KEY / JWT_SECRET_KEY is blank, too short, or still set to "
                    f"a shipped default value (environment='{self.environment}'). This is "
                    "the token-signing secret — a blank or well-known value lets anyone "
                    "forge admin sessions, so the coordinator will not start. It must be at "
                    f"least {MIN_SECRET_LENGTH} characters. Generate a strong secret with: "
                    "openssl rand -hex 32  and set API_SECRET_KEY (and/or JWT_SECRET_KEY). "
                    "For a throwaway LOCAL trial only, set ENVIRONMENT=development AND "
                    "ALLOW_INSECURE_DEV=true to boot with the insecure default."
                )
            logger.warning(
                "SECURITY: booting with the shipped DEFAULT API/JWT signing secret "
                "because ALLOW_INSECURE_DEV=true in development. Session/API tokens are "
                "signed with a PUBLIC, forgeable key. NEVER use this outside a local "
                "throwaway trial — generate real secrets with `openssl rand -hex 32`."
            )

        # Non-development gate (staging / saas / production — anything that is not
        # an explicit "development" run). The HMAC and API signing secrets must not
        # carry the shipped change-this* / dev_* placeholders outside development:
        # these defaults are well-known, so a leaked/default value lets anyone forge
        # HMAC signatures or API tokens. (The API/JWT signing key is already enforced
        # for every environment above; the HMAC key is gated here.)
        if env != "development":
            if _is_weak_secret(self.hmac_secret_key):
                raise ValueError(
                    "HMAC_SECRET_KEY is blank, too short, or still its default outside "
                    f"development (environment='{self.environment}'). It must be at least "
                    f"{MIN_SECRET_LENGTH} characters. "
                    "Generate one with: openssl rand -hex 32"
                )
            if _is_weak_secret(self.api_secret_key):
                raise ValueError(
                    "API_SECRET_KEY is blank, too short, or still its default outside "
                    f"development (environment='{self.environment}'). It must be at least "
                    f"{MIN_SECRET_LENGTH} characters. "
                    "Generate one with: openssl rand -hex 32"
                )

        # Known-compromised encryption keys. Checked in EVERY environment, not just
        # production: a development instance encrypting its vault under a publicly
        # known key is just as readable to anyone who obtains the database file.
        #
        # Stored as SHA-256 digests, never as the keys themselves — this file is
        # published in a public repository, and shipping the literal key material
        # would broadcast it to everyone who clones the repo.
        if self.secret_encryption_key:
            import hashlib

            compromised_key_digests = {
                "fef5eadc6f5145759757e7eac8f08b49eadd3f6bf1d939bcd9d291741902e3e5",
                "9d632862daae49cce5fd6858017f08814a2d4e09fed2d559736be62ed915f842",
                "158f825f08a0dd85af4d59f7bfa9402010a1feeb867d8370d9d4e47e948ef3ef",
            }
            digest = hashlib.sha256(self.secret_encryption_key.encode()).hexdigest()
            if digest in compromised_key_digests:
                raise ValueError(
                    "SECRET_ENCRYPTION_KEY is set to a known compromised value that was "
                    "committed to source control. Generate a new key with: python -c "
                    "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
                )

        if env == "production":
            # Reject a missing OR blank encryption key. `is None` alone would let
            # `SECRET_ENCRYPTION_KEY=` (the shipped .env.example line, copied but not
            # filled in) pass straight through.
            if not (self.secret_encryption_key or "").strip():
                raise ValueError(
                    "SECRET_ENCRYPTION_KEY must be set in production. "
                    "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
                )

            # api_secret_key / hmac_secret_key / jwt_secret_key are already gated by
            # the `env != "development"` block above, which is strictly stronger than
            # the old production-only `startswith("change-this")` checks (it also
            # catches blank and short values). Nothing extra is needed here.

            # Service-to-service secrets are read directly from the environment
            # (not Settings fields). An empty value makes hmac.compare_digest
            # match an empty caller-supplied secret, and the dev_* defaults are
            # well-known. Reject both in production. /internal/* can return
            # decrypted AI provider keys, so this is critical.
            for env_name in ("INTERNAL_API_SECRET", "GATEWAY_SECRET", "RECORDER_AUTH_SECRET"):
                if _is_weak_secret(os.getenv(env_name)):
                    raise ValueError(
                        f"{env_name} is blank, too short, or still a well-known default; it "
                        "must be set to a strong, non-default value in production (at least "
                        f"{MIN_SECRET_LENGTH} characters). "
                        "Generate one with: openssl rand -hex 32"
                    )

            # NOTE: the coordinator uses a local SQLite database (WRIT_DB_PATH),
            # so there are no external datastore passwords to validate here.
            # File-permission hardening of the .db is an ops concern, not a
            # secret gate.

            # CORS '*' combined with credentials lets any origin make
            # authenticated cross-origin requests. Refuse the dangerous combo.
            if "*" in self.cors_origins_list:
                raise ValueError(
                    "CORS_ORIGINS must list explicit https origins in production, not '*' "
                    "(credentials are enabled, so '*' would expose every authenticated endpoint)."
                )

            # Admin MFA is a SOFT default in production: we WANT it on, but flipping
            # it for an operator who hasn't enrolled would lock them out of the
            # admin panel on first boot. So we only WARN (never raise) — turn it on
            # via REQUIRE_ADMIN_MFA=true after every admin has a second factor.
            if not self.require_admin_mfa:
                import logging

                logging.getLogger(__name__).warning(
                    "REQUIRE_ADMIN_MFA is not set in production: platform admins are "
                    "NOT required to have a second factor. Enroll all admins (TOTP or a "
                    "passkey), then set REQUIRE_ADMIN_MFA=true to enforce it."
                )

        return self

    @property
    def database_url(self) -> str:
        """
        Async SQLAlchemy URL for the coordinator's local SQLite database.

        Derived from ``writ_db_path`` (env WRIT_DB_PATH). Kept as a property —
        not a field — so the many ``str(settings.database_url)`` call sites keep
        working without any change. The path is
        normalised to an absolute filesystem path so the URL is stable
        regardless of the process working directory.
        """
        raw = os.getenv("WRIT_DB_PATH") or self.writ_db_path
        db_path = Path(raw).expanduser()
        # SQLAlchemy's aiosqlite URL wants the leading slash of an absolute path
        # AFTER the `sqlite+aiosqlite://` authority, i.e. four slashes total for
        # an absolute path. Path.resolve() gives us that leading slash.
        return f"sqlite+aiosqlite:///{db_path.resolve()}"

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",")]

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment.lower() == "production"


def should_expose_openapi(is_production: bool) -> bool:
    """Whether to serve /openapi.json, /docs and /redoc.

    Off in production, INCLUDING the raw schema. Turning off the interactive
    viewer while still serving the spec is only half a decision: the spec is the
    part that enumerates every route, parameter and response shape, and it
    answers without authentication — a free map of the API for anyone who finds
    the host. Nothing in the product reads it at runtime (the SPA is written
    against typed clients, not generated from the schema), so nothing breaks.

    ``WRIT_EXPOSE_OPENAPI=true`` turns it back on for an operator who wants to
    generate a client against their own deployment. A separate function rather
    than an inline expression so the rule is testable without importing the app.
    """
    if not is_production:
        return True
    return os.getenv("WRIT_EXPOSE_OPENAPI", "").strip().lower() in ("1", "true", "yes")


# Global settings instance
settings = Settings()
