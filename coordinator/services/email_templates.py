"""
Email template renderer with shared base layout and i18n support.

All templates share the same visual identity: Writ logo, clean typography,
480px max-width, dark CTA buttons. Each template function returns (subject, html, text).
"""
from html import escape as html_escape
from typing import Optional, Tuple
from services.email_translations import t, DEFAULT_LOCALE


def _marketing_footer(unsubscribe_url: str, locale: str = "en") -> str:
    """CAN-SPAM / PECR footer: opt-in reason + one-click unsubscribe link + postal address.

    Only rendered for marketing/non-transactional mail (when an unsubscribe_url is supplied).
    """
    link = (
        f'<a href="{unsubscribe_url}" style="color:#6B6B6B;text-decoration:underline;">'
        f'{t("footer_unsubscribe_link", locale)}</a>'
    )
    return (
        f'{t("footer_marketing_reason", locale)}<br>'
        f'{link}<br>'
        f'{t("footer_postal_address", locale)}'
    )


def _base_layout(
    content: str,
    footer_text: str = "",
    locale: str = "en",
    unsubscribe_url: Optional[str] = None,
) -> str:
    if unsubscribe_url:
        # Marketing footer overrides the generic transactional footer text so the
        # required unsubscribe link + physical postal address are always present.
        footer = _marketing_footer(unsubscribe_url, locale)
    else:
        footer = footer_text or t("footer_unsubscribe", locale)
    return f"""\
<!DOCTYPE html>
<html lang="{locale}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#F9F9F9;">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:480px;margin:0 auto;padding:40px 20px;">
  <div style="width:36px;height:36px;background:#0D0D0D;border-radius:8px;display:flex;align-items:center;justify-content:center;margin-bottom:24px;">
    <span style="color:white;font-size:18px;font-weight:bold;">W</span>
  </div>
  {content}
  <p style="color:#A0A0A0;font-size:12px;margin-top:32px;line-height:1.5;">{footer}</p>
</div>
</body>
</html>"""


def _heading(text: str) -> str:
    return f'<h1 style="font-size:20px;font-weight:600;color:#0D0D0D;margin:0 0 8px;">{text}</h1>'


def _paragraph(text: str) -> str:
    return f'<p style="color:#6B6B6B;font-size:14px;line-height:1.6;margin:0 0 16px;">{text}</p>'


def _cta_button(text: str, url: str) -> str:
    return (
        f'<a href="{url}" style="display:inline-block;background:#0D0D0D;color:white;'
        f'padding:10px 20px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:500;">'
        f'{text}</a>'
    )


def _info_box(text: str) -> str:
    return (
        f'<div style="background:#F5F5F5;border-radius:8px;padding:16px;margin-bottom:24px;">'
        f'<p style="color:#0D0D0D;font-size:13px;margin:0;line-height:1.6;">{text}</p></div>'
    )


def _warning_box(text: str) -> str:
    return (
        f'<div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;'
        f'padding:12px 16px;margin-bottom:24px;">'
        f'<p style="color:#991B1B;font-size:13px;margin:0;">{text}</p></div>'
    )


def _detail_row(label: str, value: str) -> str:
    return (
        f'<tr><td style="color:#6B6B6B;font-size:13px;padding:6px 16px 6px 0;white-space:nowrap;">'
        f'{label}</td><td style="color:#0D0D0D;font-size:13px;padding:6px 0;font-weight:500;">'
        f'{value}</td></tr>'
    )


def _detail_table(rows: list[tuple[str, str]]) -> str:
    inner = "".join(_detail_row(l, v) for l, v in rows)
    return (
        f'<table style="margin-bottom:24px;border-collapse:collapse;" cellpadding="0" cellspacing="0">'
        f'{inner}</table>'
    )


def _url_fallback(url: str, locale: str = "en") -> str:
    label = t("verify_fallback", locale)
    return f'<p style="font-size:12px;color:#ABABAB;margin-top:8px;word-break:break-all;">{label} {url}</p>'


# ==========================================================================
# AUTH templates
# ==========================================================================

def welcome(name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    subject = t("welcome_subject", locale, name=name)
    steps = "".join(
        f'<p style="color:#0D0D0D;font-size:13px;margin:0 0 8px;"><strong>{i}.</strong> {t(f"welcome_step_{i}", locale)}</p>'
        for i in range(1, 4)
    )
    content = (
        _heading(t("welcome_heading", locale, name=name))
        + _paragraph(t("welcome_body", locale))
        + f'<div style="background:#F5F5F5;border-radius:8px;padding:16px;margin-bottom:24px;">{steps}</div>'
        + _cta_button(t("welcome_cta", locale), f"{base_url}/")
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('welcome_heading', locale, name=name)}\n\n{t('welcome_body', locale)}\n\n{base_url}/"
    return subject, html, text


def verify_email(to_email: str, token: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    url = f"{base_url}/verify-email?token={token}"
    subject = t("verify_subject", locale)
    content = (
        _heading(t("verify_heading", locale))
        + _paragraph(t("verify_body", locale))
        + _cta_button(t("verify_cta", locale), url)
        + _url_fallback(url, locale)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('verify_heading', locale)}\n\n{t('verify_body', locale)}\n\n{url}"
    return subject, html, text


def password_reset(token: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    url = f"{base_url}/reset-password?token={token}"
    subject = t("reset_subject", locale)
    content = (
        _heading(t("reset_heading", locale))
        + _paragraph(t("reset_body", locale))
        + _cta_button(t("reset_cta", locale), url)
        + f'<p style="font-size:12px;color:#ABABAB;margin-top:32px;">{t("reset_expiry", locale)}</p>'
        + _url_fallback(url, locale)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('reset_heading', locale)}\n\n{t('reset_body', locale)}\n\n{url}\n\n{t('reset_expiry', locale)}"
    return subject, html, text


def password_changed(base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    url = f"{base_url}/forgot-password"
    subject = t("password_changed_subject", locale)
    content = (
        _heading(t("password_changed_heading", locale))
        + _paragraph(t("password_changed_body", locale))
        + _cta_button(t("password_changed_cta", locale), url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('password_changed_heading', locale)}\n\n{t('password_changed_body', locale)}\n\n{url}"
    return subject, html, text


# ==========================================================================
# SUBSCRIPTION templates
# ==========================================================================

def subscription_renewed(name: str, plan_name: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    subject = t("renewed_subject", locale, plan_name=plan_name)
    content = (
        _heading(t("renewed_heading", locale))
        + _paragraph(t("renewed_body", locale, name=name, plan_name=plan_name))
    )
    html = _base_layout(content, footer_text=t("renewed_thanks", locale), locale=locale)
    text = f"{t('renewed_heading', locale)}\n\n{t('renewed_body', locale, name=name, plan_name=plan_name)}"
    return subject, html, text


def subscription_canceled(name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    billing_url = f"{base_url}/billing"
    subject = t("canceled_subject", locale, plan_name=plan_name)
    content = (
        _heading(t("canceled_heading", locale))
        + _paragraph(t("canceled_body", locale, name=name, plan_name=plan_name))
        + _info_box(
            f'<strong>{t("canceled_what_next_title", locale)}</strong><br>'
            + t("canceled_what_next_body", locale)
        )
        + _cta_button(t("canceled_cta", locale), billing_url)
    )
    html = _base_layout(content, footer_text=t("canceled_footer", locale), locale=locale)
    text = f"{t('canceled_heading', locale)}\n\n{t('canceled_body', locale, name=name, plan_name=plan_name)}\n\n{billing_url}"
    return subject, html, text


def subscription_expiring(name: str, plan_name: str, end_date: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    billing_url = f"{base_url}/billing"
    subject = t("expiring_subject", locale, plan_name=plan_name, end_date=end_date)
    content = (
        _heading(t("expiring_heading", locale))
        + _paragraph(t("expiring_body", locale, name=name, plan_name=plan_name, end_date=end_date))
        + _cta_button(t("expiring_cta", locale), billing_url)
    )
    html = _base_layout(content, footer_text=t("expiring_footer", locale), locale=locale)
    text = f"{t('expiring_heading', locale)}\n\n{t('expiring_body', locale, name=name, plan_name=plan_name, end_date=end_date)}\n\n{billing_url}"
    return subject, html, text


def plan_upgraded(name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    subject = t("upgraded_subject", locale, plan_name=plan_name)
    content = (
        _heading(t("upgraded_heading", locale))
        + _paragraph(t("upgraded_body", locale, name=name, plan_name=plan_name))
        + _cta_button(t("upgraded_cta", locale), f"{base_url}/")
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('upgraded_heading', locale)}\n\n{t('upgraded_body', locale, name=name, plan_name=plan_name)}"
    return subject, html, text


def plan_downgraded(name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    billing_url = f"{base_url}/billing"
    subject = t("downgraded_subject", locale, plan_name=plan_name)
    content = (
        _heading(t("downgraded_heading", locale))
        + _paragraph(t("downgraded_body", locale, name=name, plan_name=plan_name))
        + _cta_button(t("downgraded_cta", locale), billing_url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('downgraded_heading', locale)}\n\n{t('downgraded_body', locale, name=name, plan_name=plan_name)}"
    return subject, html, text


# ==========================================================================
# BILLING templates
# ==========================================================================

def payment_failed(name: str, plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    billing_url = f"{base_url}/billing"
    subject = t("payment_failed_subject", locale)
    content = (
        _heading(t("payment_failed_heading", locale))
        + _paragraph(t("payment_failed_body", locale, name=name, plan_name=plan_name))
        + _warning_box(t("payment_failed_warning", locale))
        + _cta_button(t("payment_failed_cta", locale), billing_url)
    )
    html = _base_layout(content, footer_text=t("payment_failed_footer", locale), locale=locale)
    text = f"{t('payment_failed_heading', locale)}\n\n{t('payment_failed_body', locale, name=name, plan_name=plan_name)}\n\n{billing_url}"
    return subject, html, text


def invoice_receipt(
    name: str,
    plan_name: str,
    amount: str,
    invoice_date: str,
    invoice_number: str,
    invoice_url: str,
    locale: str = DEFAULT_LOCALE,
) -> Tuple[str, str, str]:
    subject = t("invoice_subject", locale, invoice_number=invoice_number)
    content = (
        _heading(t("invoice_heading", locale))
        + _paragraph(t("invoice_body", locale, name=name))
        + _detail_table([
            (t("invoice_plan", locale), plan_name),
            (t("invoice_amount", locale), amount),
            (t("invoice_date", locale), invoice_date),
            (t("invoice_status", locale), t("invoice_paid", locale)),
        ])
        + _cta_button(t("invoice_cta", locale), invoice_url)
    )
    html = _base_layout(content, locale=locale)
    text = (
        f"{t('invoice_heading', locale)}\n\n"
        f"{t('invoice_body', locale, name=name)}\n\n"
        f"{t('invoice_plan', locale)}: {plan_name}\n"
        f"{t('invoice_amount', locale)}: {amount}\n"
        f"{t('invoice_date', locale)}: {invoice_date}\n\n"
        f"{invoice_url}"
    )
    return subject, html, text


# ==========================================================================
# TEAM templates
# ==========================================================================

def team_invite(org_name: str, inviter_name: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    login_url = f"{base_url}/login"
    subject = t("invite_subject", locale, org_name=org_name)
    content = (
        _heading(t("invite_heading", locale))
        + _paragraph(t("invite_body", locale, inviter_name=inviter_name, org_name=org_name))
        + _cta_button(t("invite_cta", locale), login_url)
    )
    html = _base_layout(content, footer_text=t("invite_fallback", locale), locale=locale)
    text = f"{t('invite_heading', locale)}\n\n{t('invite_body', locale, inviter_name=inviter_name, org_name=org_name)}\n\n{login_url}"
    return subject, html, text


def member_joined(
    member_name: str, member_email: str, role: str, org_name: str, base_url: str,
    locale: str = DEFAULT_LOCALE,
) -> Tuple[str, str, str]:
    team_url = f"{base_url}/settings/team"
    subject = t("member_joined_subject", locale, member_name=member_name, org_name=org_name)
    content = (
        _heading(t("member_joined_heading", locale))
        + _paragraph(t("member_joined_body", locale, member_name=member_name, member_email=member_email, role=role, org_name=org_name))
        + _cta_button(t("member_joined_cta", locale), team_url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('member_joined_heading', locale)}\n\n{member_name} ({member_email}) — {role}\n\n{team_url}"
    return subject, html, text


def member_removed(org_name: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    subject = t("member_removed_subject", locale, org_name=org_name)
    content = (
        _heading(t("member_removed_heading", locale))
        + _paragraph(t("member_removed_body", locale, org_name=org_name))
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('member_removed_heading', locale)}\n\n{t('member_removed_body', locale, org_name=org_name)}"
    return subject, html, text


def role_changed(org_name: str, old_role: str, new_role: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    workspace_url = f"{base_url}/"
    subject = t("role_changed_subject", locale, org_name=org_name)
    content = (
        _heading(t("role_changed_heading", locale))
        + _paragraph(t("role_changed_body", locale, org_name=org_name, old_role=old_role, new_role=new_role))
        + _cta_button(t("role_changed_cta", locale), workspace_url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('role_changed_heading', locale)}\n\n{old_role} → {new_role}\n\n{workspace_url}"
    return subject, html, text


# ==========================================================================
# ACCOUNT templates
# ==========================================================================

def account_deleted(locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    subject = t("account_deleted_subject", locale)
    content = (
        _heading(t("account_deleted_heading", locale))
        + _paragraph(t("account_deleted_body", locale))
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('account_deleted_heading', locale)}\n\n{t('account_deleted_body', locale)}"
    return subject, html, text


def security_alert(
    device: str, location: str, time: str, base_url: str,
    locale: str = DEFAULT_LOCALE,
) -> Tuple[str, str, str]:
    subject = t("security_alert_subject", locale)
    content = (
        _heading(t("security_alert_heading", locale))
        + _paragraph(t("security_alert_body", locale))
        + _detail_table([
            (t("security_alert_device", locale), device),
            (t("security_alert_location", locale), location),
            (t("security_alert_time", locale), time),
        ])
        + _warning_box(t("security_alert_warning", locale))
        + _cta_button(t("security_alert_cta", locale), f"{base_url}/settings/security")
    )
    html = _base_layout(content, locale=locale)
    text = (
        f"{t('security_alert_heading', locale)}\n\n"
        f"{t('security_alert_body', locale)}\n"
        f"{device} — {location} — {time}\n\n"
        f"{t('security_alert_warning', locale)}"
    )
    return subject, html, text


def usage_warning(
    name: str, resource: str, percent: int, used: str, total: str,
    plan_name: str, base_url: str, locale: str = DEFAULT_LOCALE,
) -> Tuple[str, str, str]:
    billing_url = f"{base_url}/billing"
    subject = t("usage_warning_subject", locale, percent=percent, resource=resource)
    content = (
        _heading(t("usage_warning_heading", locale))
        + _paragraph(t("usage_warning_body", locale, name=name, percent=percent, resource=resource, plan_name=plan_name, used=used, total=total))
        + _cta_button(t("usage_warning_cta", locale), billing_url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('usage_warning_heading', locale)}\n\n{resource}: {used}/{total} ({percent}%)\n\n{billing_url}"
    return subject, html, text


# ==========================================================================
# SUPPORT templates
# ==========================================================================

def ticket_created(
    ticket_id: str, ticket_subject: str, base_url: str,
    locale: str = DEFAULT_LOCALE,
) -> Tuple[str, str, str]:
    ticket_url = f"{base_url}/support/tickets/{ticket_id}"
    subject = t("ticket_created_subject", locale, ticket_id=ticket_id)
    content = (
        _heading(t("ticket_created_heading", locale))
        + _paragraph(t("ticket_created_body", locale))
        + _detail_table([
            (t("ticket_created_ref", locale), f"#{ticket_id}"),
            (t("ticket_created_subject_label", locale), html_escape(ticket_subject)),
        ])
        + _cta_button(t("ticket_created_cta", locale), ticket_url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('ticket_created_heading', locale)}\n\n#{ticket_id}: {ticket_subject}\n\n{ticket_url}"
    return subject, html, text


def ticket_reply(ticket_id: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    ticket_url = f"{base_url}/support/tickets/{ticket_id}"
    subject = t("ticket_reply_subject", locale, ticket_id=ticket_id)
    content = (
        _heading(t("ticket_reply_heading", locale))
        + _paragraph(t("ticket_reply_body", locale, ticket_id=ticket_id))
        + _cta_button(t("ticket_reply_cta", locale), ticket_url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('ticket_reply_heading', locale)}\n\n#{ticket_id}\n\n{ticket_url}"
    return subject, html, text


# ==========================================================================
# QUOTE templates
# ==========================================================================

def quote_sent(
    name: str, quote_ref: str, valid_until: str, quote_url: str,
    locale: str = DEFAULT_LOCALE,
) -> Tuple[str, str, str]:
    subject = t("quote_sent_subject", locale, quote_ref=quote_ref)
    content = (
        _heading(t("quote_sent_heading", locale))
        + _paragraph(t("quote_sent_body", locale, name=name, valid_until=valid_until))
        + _cta_button(t("quote_sent_cta", locale), quote_url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('quote_sent_heading', locale)}\n\n{t('quote_sent_body', locale, name=name, valid_until=valid_until)}\n\n{quote_url}"
    return subject, html, text


def quote_accepted(name: str, quote_ref: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    subject = t("quote_accepted_subject", locale, quote_ref=quote_ref)
    content = (
        _heading(t("quote_accepted_heading", locale))
        + _paragraph(t("quote_accepted_body", locale, name=name, quote_ref=quote_ref))
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('quote_accepted_heading', locale)}\n\n{t('quote_accepted_body', locale, name=name, quote_ref=quote_ref)}"
    return subject, html, text


def quote_expired(name: str, quote_ref: str, base_url: str, locale: str = DEFAULT_LOCALE) -> Tuple[str, str, str]:
    contact_url = f"{base_url}/contact"
    subject = t("quote_expired_subject", locale, quote_ref=quote_ref)
    content = (
        _heading(t("quote_expired_heading", locale))
        + _paragraph(t("quote_expired_body", locale, name=name, quote_ref=quote_ref))
        + _cta_button(t("quote_expired_cta", locale), contact_url)
    )
    html = _base_layout(content, locale=locale)
    text = f"{t('quote_expired_heading', locale)}\n\n{t('quote_expired_body', locale, name=name, quote_ref=quote_ref)}\n\n{contact_url}"
    return subject, html, text
