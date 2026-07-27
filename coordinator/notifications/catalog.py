"""
SELF-HOST platform notification event catalog — the single-owner coordinator's
trimmed counterpart of the cloud catalog (the hosted notification catalog).

Bridges in-app notification types and per-owner channel preferences
(`models/user_notification_preference.py`). Only events the coordinator
ACTUALLY emits are listed (no billing/team/marketplace/support — those are
cloud-only surfaces). Event keys and channel names MUST stay aligned with the
cloud catalog so the two deployments never drift apart.

Deliberately excluded: `monitors.monitor_down` / `monitors.monitor_recovered`.
The coordinator's monitor-health transitions (services/monitor_health_events.py)
only feed trigger automations (UnifiedTriggerService) — they create no in-app
notification and send no provider notification, so there is nothing for a
preference matrix to gate. If a future change routes them through
`services.platform_notifier`, add the events here with the cloud-aligned keys.

Scope: PLATFORM-WIDE events. Per-monitor change alerts keep their own
per-target config (`target.notification_providers` + operator recipient tables)
and are deliberately NOT part of this catalog.

Shape of an event entry:
    "runs.run_failed": {
        "category": "runs",
        "label": "Run failed",              # English source string (frontend t()'s it)
        "description": "...",
        "types": ("run_failed",),           # legacy Notification.type strings that map here
        "channels": {                        # ONLY listed channels are offered for the event
            "in_app":  {"default": True,  "locked": False},
            "email":   {"default": False, "locked": False},
            ...
        },
    }

`locked: True` means the channel is always on and not user-disableable. The
preferences API refuses to store an override for a locked cell;
`platform_notifier` treats locked as enabled regardless of stored preferences.
"""
from typing import Any, Dict, Optional

# Channels a PLATFORM event can be delivered on, for the owner.
#   in_app   — Notification row (bell + inbox)
#   email    — the owner's account email (User.email), via the coordinator's
#              own SMTP config (models/email_config.py + notifications/email.py)
#   sms      — Twilio SMS to the owner's personal phone (preferences.phone_number)
#   whatsapp — WhatsApp via Twilio to the same personal phone
#   signal   — Signal to the same personal phone
#   pushover — the owner's personal Pushover key (preferences.pushover_user_key)
CHANNELS = ("in_app", "email", "sms", "whatsapp", "signal", "pushover")

# Channels that deliver to a personal contact point stored on the preference row.
PHONE_CHANNELS = ("sms", "whatsapp", "signal")

CATEGORIES = {
    "runs": "Automations & runs",
    "agents": "Agents & devices",
}


def _ch(default_on=(), locked=(), offered=CHANNELS) -> Dict[str, Dict[str, bool]]:
    """Build a channel map: every offered channel, defaulting on/locked as given."""
    out = {}
    for ch in offered:
        out[ch] = {"default": ch in default_on or ch in locked, "locked": ch in locked}
    return out


EVENTS: Dict[str, Dict[str, Any]] = {
    # ---------------------------------------------------------------------- runs
    "runs.run_failed": {
        "category": "runs",
        "label": "Run failed",
        "description": "An automation or workflow run ended in failure.",
        "types": ("run_failed",),
        "channels": _ch(default_on=("in_app",)),
    },
    # -------------------------------------------------------------------- agents
    "agents.agent_connected": {
        "category": "agents",
        "label": "New device linked",
        "description": "A new machine connected to your coordinator as an agent.",
        "types": ("agent_connected",),
        "channels": _ch(default_on=("in_app", "email")),
    },
}

# Legacy Notification.type → catalog event key (built from EVENTS[*]["types"]).
TYPE_TO_EVENT: Dict[str, str] = {
    t: key for key, ev in EVENTS.items() for t in ev["types"]
}


def resolve_event(event_or_type: str) -> Optional[str]:
    """Accept either a catalog key ("runs.run_failed") or a legacy
    Notification.type ("run_failed"); return the catalog key or None."""
    if event_or_type in EVENTS:
        return event_or_type
    return TYPE_TO_EVENT.get(event_or_type)


def event_channels(event_key: str) -> Dict[str, Dict[str, bool]]:
    return EVENTS[event_key]["channels"]


def effective_channels(event_key: str, stored: Optional[dict]) -> Dict[str, bool]:
    """Merge the owner's stored preference map for one event over catalog defaults.

    `stored` is the per-event dict out of UserNotificationPreference.preferences
    (may be None or partial). Locked channels are always True. Channels the
    event does not offer are absent from the result.
    """
    spec = EVENTS[event_key]["channels"]
    stored = stored or {}
    out: Dict[str, bool] = {}
    for ch, meta in spec.items():
        if meta["locked"]:
            out[ch] = True
        elif isinstance(stored.get(ch), bool):
            out[ch] = stored[ch]
        else:
            out[ch] = meta["default"]
    return out


def sanitize_preferences(raw: dict) -> dict:
    """Validate/trim a client-submitted preference matrix for storage.

    Keeps only known events and offered, non-locked channels with boolean
    values. Unknown keys are dropped silently (forward/backward compat)."""
    clean: dict = {}
    if not isinstance(raw, dict):
        return clean
    for event_key, chans in raw.items():
        spec = EVENTS.get(event_key, {}).get("channels")
        if not spec or not isinstance(chans, dict):
            continue
        kept = {
            ch: val
            for ch, val in chans.items()
            if ch in spec and not spec[ch]["locked"] and isinstance(val, bool)
        }
        if kept:
            clean[event_key] = kept
    return clean
