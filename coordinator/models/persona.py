"""
Persona — a reusable authenticated identity attached to workflows.

A Persona bundles, for one site/account:
  - login credentials (Fernet-encrypted)
  - a 2FA method + secret (TOTP seed, or a connected mailbox / relay for email-OTP)
  - a consistent browser fingerprint
  - residential-IP / trusted-agent affinity
  - a warm auth session (cookies/localStorage) shared across all workflows using it

Personas belong to the single owner. A workflow with a persona is dispatched to
a trusted residential agent at execution time.

Secret material (password, TOTP seed, mail OAuth tokens, proxy creds, session
state) is Fernet-encrypted with the shared SECRET_ENCRYPTION_KEY and is NEVER
returned via the API. See security/encryption.py.
"""
from sqlalchemy import (
    JSON,
    Column, String, Text, Integer, Boolean, DateTime, ForeignKey,
    UniqueConstraint, Index,
)
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from database import Base


class Persona(Base):
    __tablename__ = "personas"

    id = Column(Integer, primary_key=True, autoincrement=True)

    name = Column(String(120), nullable=False, comment="Human label, e.g. 'Acme prod login'")
    description = Column(Text, nullable=True)
    target_domain = Column(
        String(255), nullable=True, index=True,
        comment="Registrable domain this persona authenticates against; used to suggest/filter by workflow entry_url",
    )

    # --- Login credentials ---
    login_username = Column(
        String(255), nullable=True,
        comment="Plaintext login identifier (email/handle) — non-secret, shown in UI",
    )
    credentials_encrypted = Column(
        Text, nullable=True,
        comment="Fernet-encrypted JSON {password, ...extra login fields}",
    )

    # --- 2FA ---
    twofa_method = Column(
        String(20), nullable=False, server_default="none",
        comment="none | totp | email_otp | sms",
    )
    # TOTP: encrypted base32 seed (code generated server-side at the /otp callback)
    totp_seed_encrypted = Column(Text, nullable=True, comment="Fernet-encrypted TOTP base32 secret")
    totp_digits = Column(Integer, nullable=False, server_default="6")
    totp_period_seconds = Column(Integer, nullable=False, server_default="30")
    totp_algorithm = Column(String(10), nullable=False, server_default="SHA1")

    # EMAIL-OTP: either a connected OAuth mailbox or a forwarding relay address
    email_otp_mode = Column(
        String(20), nullable=True,
        comment="oauth_mailbox | relay (null unless twofa_method == email_otp)",
    )
    # A plain nullable int retained for data-shape compatibility; the
    # oauth-mailbox OTP mode is not wired in the coordinator.
    mail_connection_id = Column(
        Integer,
        nullable=True, comment="(legacy) connected OAuth mailbox id; unused in coordinator",
    )
    relay_address = Column(
        String(320), nullable=True,
        comment="Forwarding-relay address that receives the OTP — an email inbox (email_otp relay) "
                "or a phone number/relay token (sms). Inbound messages arrive via POST /api/relay/inbound.",
    )
    otp_extract_config = Column(
        JSON, nullable=True,
        comment="How to parse the OTP/magic-link: {from_filter, subject_regex, code_regex, link_regex, max_age_seconds}",
    )

    # --- Browser fingerprint (consistent per persona) ---
    fingerprint = Column(
        JSON, nullable=True,
        comment="Pinned {user_agent, locale, timezone} matching the agent Fingerprint shape",
    )

    # --- Residential-IP affinity ---
    preferred_agent_id = Column(
        String(255), nullable=True, index=True,
        comment="Preferred trusted/residential agent (matches agents.agent_id)",
    )
    proxy_config_encrypted = Column(
        Text, nullable=True,
        comment="Fernet-encrypted residential proxy {server,username,password}",
    )
    proxy_lawful_use_ack_at = Column(
        DateTime(timezone=True), nullable=True,
        comment="When the owner acknowledged lawful use of the configured BYO residential proxy. "
                "Stamped whenever a proxy is set; required to store proxy creds.",
    )
    ip_affinity = Column(
        JSON, nullable=True,
        comment="Optional pinned egress info {country, region, sticky_session_id}",
    )

    # --- Warm auth session (mirrors WorkflowAgentAffinity, but identity-scoped) ---
    session_state_encrypted = Column(
        Text, nullable=True,
        comment="gzip+Fernet auth_session {cookies,localStorage,sessionStorage,headers,fingerprint}",
    )
    earliest_cookie_expiry = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    validation_status = Column(
        String(20), nullable=False, server_default="unknown",
        comment="unknown | valid | expired",
    )
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    # --- Login workflow (how this persona SIGNS IN) ---
    # Without this the warm session could only ever be CAPTURED from something that
    # had already logged in; a persona created with credentials alone had no way to
    # establish one, so authenticated crawls using it were rejected forever.
    # Dispatching this workflow with persona_id folds in the credentials + 2FA, and
    # the run-completion write-back persists the captured session onto the persona.
    # SET NULL on delete: losing the workflow must never delete the identity.
    # `use_alter` breaks a FOREIGN-KEY CYCLE. `automation_workflows.default_persona_id`
    # already points here, so this column makes the two tables mutually dependent and
    # SQLAlchemy can no longer order them:
    #   "Cannot correctly sort tables; there are unresolvable cycles between tables
    #    automation_workflows, personas"
    # which breaks `metadata.create_all` / `drop_all` outright — and SQLAlchemy warns
    # that it "may raise an error in a future release". `use_alter` emits this one
    # constraint separately instead of inline, so the cycle no longer participates in
    # the sort. It needs an explicit `name` to be alterable at all.
    #
    # Migrations are unaffected either way: 0017 adds the column to a table that
    # already exists, so it never sorts the schema as a whole.
    login_workflow_id = Column(
        Integer,
        ForeignKey(
            "automation_workflows.id", ondelete="SET NULL",
            use_alter=True, name="fk_personas_login_workflow_id",
        ),
        nullable=True, index=True,
        comment="Workflow that signs this persona in; re-run on demand and on session expiry",
    )
    last_login_error = Column(
        Text, nullable=True,
        comment="Why the most recent sign-in attempt failed (cleared on success)",
    )

    is_active = Column(Boolean, nullable=False, server_default="true", index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("name", name="uq_persona_name"),
        Index("ix_personas_domain", "target_domain"),
    )

    def __repr__(self) -> str:
        return f"<Persona(id={self.id}, name='{self.name}', domain='{self.target_domain}', twofa='{self.twofa_method}')>"
