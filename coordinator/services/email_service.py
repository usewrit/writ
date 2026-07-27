"""
Email service — SMTP-based transactional email sending.

All templates are localized (EN/FR/ES) via email_templates + email_translations.
Falls back to logging if SMTP is not configured.
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from config import settings
from services import email_templates as tpl

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "en"

# Purpose namespace for the marketing unsubscribe token so a token minted for one
# purpose can never be replayed against another HMAC-signed flow.
UNSUBSCRIBE_TOKEN_PURPOSE = "unsubscribe"


# ---------------------------------------------------------------------------
# Marketing unsubscribe token (stateless HMAC — no DB column to store/expire)
# ---------------------------------------------------------------------------

def _unsubscribe_secret() -> bytes:
    return (settings.hmac_secret_key or "").encode("utf-8")


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_unsubscribe_token(user_id: str) -> str:
    """Mint a stateless, signed one-click-unsubscribe token for a user.

    Format: "<b64url(user_id)>.<b64url(hmac_sha256)>". The token carries no expiry
    (an unsubscribe link should keep working) and is verified by
    routers.unsubscribe.verify_unsubscribe_token using the same HMAC secret.
    """
    uid = str(user_id)
    payload = f"{UNSUBSCRIBE_TOKEN_PURPOSE}:{uid}".encode("utf-8")
    sig = hmac.new(_unsubscribe_secret(), payload, hashlib.sha256).digest()
    return f"{_b64u_encode(uid.encode('utf-8'))}.{_b64u_encode(sig)}"


def build_unsubscribe_url(base_url: str, user_id: str) -> str:
    """Build the public one-click unsubscribe URL for a recipient."""
    token = generate_unsubscribe_token(user_id)
    return f"{base_url.rstrip('/')}/unsubscribe?token={token}"


# ---------------------------------------------------------------------------
# SMTP transport
# ---------------------------------------------------------------------------

def _get_smtp_config() -> Optional[dict]:
    host = getattr(settings, "smtp_host", None)
    port = getattr(settings, "smtp_port", 587)
    username = getattr(settings, "smtp_username", None)
    password = getattr(settings, "smtp_password", None)
    from_email = getattr(settings, "smtp_from_email", None) or username
    from_name = getattr(settings, "smtp_from_name", "Writ")

    if not host or not username:
        return None

    return {
        "host": host,
        "port": int(port),
        "username": username,
        "password": password,
        "from_email": from_email,
        "from_name": from_name,
        "use_tls": getattr(settings, "smtp_use_tls", True),
    }


def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
    *,
    headers: Optional[dict] = None,
    unsubscribe_url: Optional[str] = None,
    category: Optional[str] = None,
) -> bool:
    """Send a transactional or marketing email.

    Extra params (all optional, additive):
    - headers: arbitrary extra MIME headers to attach (e.g. List-Id).
    - unsubscribe_url: one-click HTTP unsubscribe endpoint. When supplied, the
      List-Unsubscribe + List-Unsubscribe-Post:One-Click headers are emitted so
      mail clients render a native unsubscribe affordance (CAN-SPAM / RFC 8058).
    - category: a free-form tag (e.g. "marketing", "transactional") surfaced as an
      X-Writ-Category header for ESP routing/analytics.
    """
    config = _get_smtp_config()

    if not config:
        logger.info(f"[EMAIL-FALLBACK] To: {to_email} | Subject: {subject}")
        logger.info(f"[EMAIL-FALLBACK] Body: {(text_body or html_body)[:200]}")
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{config['from_name']} <{config['from_email']}>"
        msg["To"] = to_email

        if category:
            msg["X-Writ-Category"] = category

        # One-click unsubscribe (RFC 2369 / RFC 8058) — lets Gmail/Outlook render a
        # native unsubscribe button and POST a one-click opt-out. Required for
        # CAN-SPAM-compliant marketing mail.
        if unsubscribe_url:
            msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        if headers:
            for hk, hv in headers.items():
                if hv is not None and hk not in msg:
                    msg[hk] = str(hv)

        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        if config["use_tls"]:
            server = smtplib.SMTP(config["host"], config["port"])
            server.ehlo()
            server.starttls()
        else:
            server = smtplib.SMTP(config["host"], config["port"])

        if config["password"]:
            server.login(config["username"], config["password"])

        server.sendmail(config["from_email"], to_email, msg.as_string())
        server.quit()

        logger.info(f"Email sent to {to_email}: {subject}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False


async def send_email_async(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
    *,
    headers: Optional[dict] = None,
    unsubscribe_url: Optional[str] = None,
    category: Optional[str] = None,
) -> bool:
    import functools
    loop = asyncio.get_event_loop()
    fn = functools.partial(
        send_email,
        to_email,
        subject,
        html_body,
        text_body,
        headers=headers,
        unsubscribe_url=unsubscribe_url,
        category=category,
    )
    return await loop.run_in_executor(None, fn)


# ---------------------------------------------------------------------------
# Helper: send a rendered template tuple (subject, html, text)
# ---------------------------------------------------------------------------

def _send_template(to_email: str, rendered: tuple[str, str, str]) -> bool:
    subject, html, text = rendered
    return send_email(to_email, subject, html, text)


async def _send_template_async(to_email: str, rendered: tuple[str, str, str]) -> bool:
    subject, html, text = rendered
    return await send_email_async(to_email, subject, html, text)


# ==========================================================================
# AUTH emails
# ==========================================================================

def send_welcome_email(to_email: str, user_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.welcome(name, base_url, locale))


def send_verification_email(to_email: str, verification_token: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.verify_email(to_email, verification_token, base_url, locale))


def send_password_reset_email(to_email: str, reset_token: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.password_reset(reset_token, base_url, locale))


def send_password_changed_email(to_email: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.password_changed(base_url, locale))


# ==========================================================================
# SUBSCRIPTION emails
# ==========================================================================

def send_subscription_renewed_email(to_email: str, user_name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.subscription_renewed(name, plan_name, locale))


def send_subscription_canceled_email(to_email: str, user_name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.subscription_canceled(name, plan_name, base_url, locale))


def send_subscription_expiring_email(to_email: str, user_name: str, plan_name: str, end_date: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.subscription_expiring(name, plan_name, end_date, base_url, locale))


def send_plan_upgraded_email(to_email: str, user_name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.plan_upgraded(name, plan_name, base_url, locale))


def send_plan_downgraded_email(to_email: str, user_name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.plan_downgraded(name, plan_name, base_url, locale))


# ==========================================================================
# BILLING emails
# ==========================================================================

def send_payment_overdue_email(to_email: str, user_name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.payment_failed(name, plan_name, base_url, locale))


def send_invoice_email(
    to_email: str, user_name: str, plan_name: str, amount: str,
    invoice_date: str, invoice_number: str, invoice_url: str,
    locale: str = DEFAULT_LOCALE,
) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.invoice_receipt(name, plan_name, amount, invoice_date, invoice_number, invoice_url, locale))


# ==========================================================================
# TEAM emails
# ==========================================================================

def send_team_invite_email(to_email: str, org_name: str, inviter_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.team_invite(org_name, inviter_name, base_url, locale))


def send_member_joined_email(
    to_email: str, member_name: str, member_email: str, role: str,
    org_name: str, base_url: str, locale: str = DEFAULT_LOCALE,
) -> bool:
    return _send_template(to_email, tpl.member_joined(member_name, member_email, role, org_name, base_url, locale))


def send_member_removed_email(to_email: str, org_name: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.member_removed(org_name, locale))


def send_role_changed_email(to_email: str, org_name: str, old_role: str, new_role: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.role_changed(org_name, old_role, new_role, base_url, locale))


# ==========================================================================
# ACCOUNT emails
# ==========================================================================

def send_account_deleted_email(to_email: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.account_deleted(locale))


def send_security_alert_email(
    to_email: str, device: str, location: str, time: str,
    base_url: str, locale: str = DEFAULT_LOCALE,
) -> bool:
    return _send_template(to_email, tpl.security_alert(device, location, time, base_url, locale))


def send_usage_warning_email(
    to_email: str, user_name: str, resource: str, percent: int,
    used: str, total: str, plan_name: str, base_url: str,
    locale: str = DEFAULT_LOCALE,
) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.usage_warning(name, resource, percent, used, total, plan_name, base_url, locale))


# ==========================================================================
# SUPPORT emails
# ==========================================================================

def send_ticket_created_email(to_email: str, ticket_id: str, ticket_subject: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.ticket_created(ticket_id, ticket_subject, base_url, locale))


def send_ticket_reply_email(to_email: str, ticket_id: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    return _send_template(to_email, tpl.ticket_reply(ticket_id, base_url, locale))


# ==========================================================================
# QUOTE emails
# ==========================================================================

def send_quote_email(
    to_email: str, user_name: str, quote_ref: str, valid_until: str,
    quote_url: str, locale: str = DEFAULT_LOCALE,
) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.quote_sent(name, quote_ref, valid_until, quote_url, locale))


def send_quote_accepted_email(to_email: str, user_name: str, quote_ref: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.quote_accepted(name, quote_ref, locale))


def send_quote_expired_email(to_email: str, user_name: str, quote_ref: str, base_url: str, locale: str = DEFAULT_LOCALE) -> bool:
    name = user_name or to_email.split("@")[0]
    return _send_template(to_email, tpl.quote_expired(name, quote_ref, base_url, locale))
