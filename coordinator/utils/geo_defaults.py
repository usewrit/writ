"""
Country -> sensible UI/billing defaults (country-aware registration).

Backs ``GET /geo/defaults`` (routers/geo.py): given the detected/selected country
on the Register page, suggest a default currency (ISO 4217), UI language, and IANA
timezone so defaults are pre-localized. These are *suggestions* only — the
user can override, and nothing here gates billing (Stripe Tax derives tax from the
customer address, not this currency hint).

IMPORTANT: ``language`` is constrained to the three UI languages Writ ships
(en / fr / es). For countries whose primary language we don't support, we fall back
to the closest supported one (e.g. DE -> en, JP -> en, BR -> es). Unknown countries
get the global default ``{currency: 'USD', language: 'en', timezone: None}``.
"""
from typing import Optional


# Only these three are renderable by the frontend (react-i18next en/fr/es).
SUPPORTED_LANGUAGES = ("en", "fr", "es")

_GLOBAL_DEFAULT = {"currency": "USD", "language": "en", "timezone": None}


# Major-market defaults. currency = ISO 4217, language ∈ {en,fr,es},
# timezone = representative IANA zone (None where a country spans many).
_DEFAULTS: dict[str, dict] = {
    # North America
    "US": {"currency": "USD", "language": "en", "timezone": "America/New_York"},
    "CA": {"currency": "CAD", "language": "en", "timezone": "America/Toronto"},
    "MX": {"currency": "MXN", "language": "es", "timezone": "America/Mexico_City"},
    # Latin America (Spanish-speaking -> es; Brazil -> pt unsupported -> es)
    "BR": {"currency": "BRL", "language": "es", "timezone": "America/Sao_Paulo"},
    "AR": {"currency": "ARS", "language": "es", "timezone": "America/Argentina/Buenos_Aires"},
    "CL": {"currency": "CLP", "language": "es", "timezone": "America/Santiago"},
    "CO": {"currency": "COP", "language": "es", "timezone": "America/Bogota"},
    "PE": {"currency": "PEN", "language": "es", "timezone": "America/Lima"},
    # Eurozone / Europe
    "FR": {"currency": "EUR", "language": "fr", "timezone": "Europe/Paris"},
    "ES": {"currency": "EUR", "language": "es", "timezone": "Europe/Madrid"},
    "DE": {"currency": "EUR", "language": "en", "timezone": "Europe/Berlin"},
    "IT": {"currency": "EUR", "language": "en", "timezone": "Europe/Rome"},
    "NL": {"currency": "EUR", "language": "en", "timezone": "Europe/Amsterdam"},
    "BE": {"currency": "EUR", "language": "fr", "timezone": "Europe/Brussels"},
    "IE": {"currency": "EUR", "language": "en", "timezone": "Europe/Dublin"},
    "PT": {"currency": "EUR", "language": "es", "timezone": "Europe/Lisbon"},
    "AT": {"currency": "EUR", "language": "en", "timezone": "Europe/Vienna"},
    "FI": {"currency": "EUR", "language": "en", "timezone": "Europe/Helsinki"},
    "GR": {"currency": "EUR", "language": "en", "timezone": "Europe/Athens"},
    "LU": {"currency": "EUR", "language": "fr", "timezone": "Europe/Luxembourg"},
    "GB": {"currency": "GBP", "language": "en", "timezone": "Europe/London"},
    "CH": {"currency": "CHF", "language": "fr", "timezone": "Europe/Zurich"},
    "SE": {"currency": "SEK", "language": "en", "timezone": "Europe/Stockholm"},
    "NO": {"currency": "NOK", "language": "en", "timezone": "Europe/Oslo"},
    "DK": {"currency": "DKK", "language": "en", "timezone": "Europe/Copenhagen"},
    "PL": {"currency": "PLN", "language": "en", "timezone": "Europe/Warsaw"},
    "CZ": {"currency": "CZK", "language": "en", "timezone": "Europe/Prague"},
    "RO": {"currency": "RON", "language": "en", "timezone": "Europe/Bucharest"},
    "HU": {"currency": "HUF", "language": "en", "timezone": "Europe/Budapest"},
    # Asia-Pacific
    "JP": {"currency": "JPY", "language": "en", "timezone": "Asia/Tokyo"},
    "CN": {"currency": "CNY", "language": "en", "timezone": "Asia/Shanghai"},
    "IN": {"currency": "INR", "language": "en", "timezone": "Asia/Kolkata"},
    "AU": {"currency": "AUD", "language": "en", "timezone": "Australia/Sydney"},
    "NZ": {"currency": "NZD", "language": "en", "timezone": "Pacific/Auckland"},
    "SG": {"currency": "SGD", "language": "en", "timezone": "Asia/Singapore"},
    "HK": {"currency": "HKD", "language": "en", "timezone": "Asia/Hong_Kong"},
    "KR": {"currency": "KRW", "language": "en", "timezone": "Asia/Seoul"},
    "ID": {"currency": "IDR", "language": "en", "timezone": "Asia/Jakarta"},
    "MY": {"currency": "MYR", "language": "en", "timezone": "Asia/Kuala_Lumpur"},
    "TH": {"currency": "THB", "language": "en", "timezone": "Asia/Bangkok"},
    "PH": {"currency": "PHP", "language": "en", "timezone": "Asia/Manila"},
    "VN": {"currency": "VND", "language": "en", "timezone": "Asia/Ho_Chi_Minh"},
    # Middle East / Africa
    "AE": {"currency": "AED", "language": "en", "timezone": "Asia/Dubai"},
    "SA": {"currency": "SAR", "language": "en", "timezone": "Asia/Riyadh"},
    "IL": {"currency": "ILS", "language": "en", "timezone": "Asia/Jerusalem"},
    "TR": {"currency": "TRY", "language": "en", "timezone": "Europe/Istanbul"},
    "ZA": {"currency": "ZAR", "language": "en", "timezone": "Africa/Johannesburg"},
    "NG": {"currency": "NGN", "language": "en", "timezone": "Africa/Lagos"},
    "EG": {"currency": "EGP", "language": "en", "timezone": "Africa/Cairo"},
    "KE": {"currency": "KES", "language": "en", "timezone": "Africa/Nairobi"},
}


def defaults_for_country(code: Optional[str]) -> dict:
    """Return ``{currency, language, timezone}`` defaults for an ISO-2 country.

    Case-insensitive. Unknown / None / malformed codes return the global default
    (USD / en / None timezone). The returned ``language`` is always one of the
    three supported UI languages. Never raises; always returns a fresh dict.
    """
    if not code or not isinstance(code, str):
        return dict(_GLOBAL_DEFAULT)
    entry = _DEFAULTS.get(code.strip().upper())
    if not entry:
        return dict(_GLOBAL_DEFAULT)
    out = dict(entry)
    # Defensive: never leak an unsupported language even if the table drifts.
    if out.get("language") not in SUPPORTED_LANGUAGES:
        out["language"] = "en"
    return out
