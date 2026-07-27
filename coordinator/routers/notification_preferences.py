"""
Owner platform notification preferences API (self-host coordinator).

GET/PUT /notifications/preferences — the event × channel matrix governing
PLATFORM-WIDE notifications (runs, agents) for the authenticated owner, plus
personal delivery contact points (phone, Pushover key).

Single-owner carve of the cloud notification-preferences router:
no organization context and no marketing consent (the coordinator sends no
marketing email). Same request/response shape otherwise so the frontends stay
aligned.

Distinct from routers/notifications.py (provider config for per-monitor change
alerts) and routers/notifications_inbox.py (the inbox). Enforcement lives in
services/platform_notifier.py; the catalog of events, defaults and locked
channels in notifications/catalog.py.
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from notifications import catalog
from models.user_notification_preference import UserNotificationPreference
from security.dependencies import get_auth_context, AuthContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["Notification Preferences"])

_PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")


# ============================================================================
# Schemas
# ============================================================================

class ChannelSpec(BaseModel):
    default: bool
    locked: bool


class EventSpec(BaseModel):
    key: str
    category: str
    label: str
    description: str
    channels: dict[str, ChannelSpec]


class CatalogResponse(BaseModel):
    categories: dict[str, str]
    channels: list[str]
    events: list[EventSpec]


class PreferencesResponse(BaseModel):
    catalog: CatalogResponse
    # Effective (defaults merged with stored overrides): event → channel → bool
    effective: dict[str, dict[str, bool]]
    # Raw stored overrides only (what the owner explicitly changed)
    overrides: dict[str, dict[str, bool]]
    channel_availability: dict[str, bool]
    phone_number: Optional[str] = None
    pushover_user_key_set: bool = False


class PreferencesUpdateRequest(BaseModel):
    # Partial update: only supplied events are touched; each event's dict
    # REPLACES that event's stored overrides (send {} to reset to defaults).
    preferences: Optional[dict[str, dict[str, bool]]] = None
    phone_number: Optional[str] = Field(
        None, max_length=32,
        description='E.164 phone for SMS/WhatsApp/Signal; empty string clears it',
    )
    pushover_user_key: Optional[str] = Field(
        None, max_length=64,
        description="Personal Pushover user key; empty string clears it",
    )


# ============================================================================
# Helpers
# ============================================================================

def _catalog_response() -> CatalogResponse:
    return CatalogResponse(
        categories=catalog.CATEGORIES,
        channels=list(catalog.CHANNELS),
        events=[
            EventSpec(
                key=key,
                category=ev["category"],
                label=ev["label"],
                description=ev["description"],
                channels={ch: ChannelSpec(**meta) for ch, meta in ev["channels"].items()},
            )
            for key, ev in catalog.EVENTS.items()
        ],
    )


async def _channel_availability(db: AsyncSession) -> dict[str, bool]:
    """Which delivery channels this coordinator can actually use right now,
    i.e. which provider configs are set up AND enabled.

    Purely informational for the UI (preferences are stored regardless, so a
    channel configured later starts working without the owner re-toggling)."""
    availability = {ch: False for ch in catalog.CHANNELS}
    availability["in_app"] = True

    async def _enabled(model) -> bool:
        try:
            row = (await db.execute(select(model).limit(1))).scalar_one_or_none()
            return bool(row and row.enabled)
        except Exception:
            return False

    from models.email_config import EmailConfig
    from models.twilio_config import TwilioConfig
    from models.whatsapp_config import WhatsAppConfig
    from models.signal_config import SignalConfig
    from models.pushover_config import PushoverConfig

    availability["email"] = await _enabled(EmailConfig)
    availability["sms"] = await _enabled(TwilioConfig)
    # WhatsApp rides on the Twilio credentials, so it needs both configs.
    availability["whatsapp"] = availability["sms"] and await _enabled(WhatsAppConfig)
    availability["signal"] = await _enabled(SignalConfig)
    availability["pushover"] = await _enabled(PushoverConfig)
    return availability


def _require_user(auth: AuthContext):
    if not auth.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Notification preferences are per-user; authenticate as a user (not a service API key)",
        )


async def _get_or_none(
    db: AsyncSession, user_id
) -> Optional[UserNotificationPreference]:
    result = await db.execute(
        select(UserNotificationPreference).where(
            UserNotificationPreference.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


def _response(row: Optional[UserNotificationPreference], availability: dict) -> PreferencesResponse:
    stored = (row.preferences or {}) if row else {}
    effective = {
        key: catalog.effective_channels(key, stored.get(key))
        for key in catalog.EVENTS
    }
    return PreferencesResponse(
        catalog=_catalog_response(),
        effective=effective,
        overrides=catalog.sanitize_preferences(stored),
        channel_availability=availability,
        phone_number=row.phone_number if row else None,
        pushover_user_key_set=bool(row.pushover_user_key) if row else False,
    )


# ============================================================================
# Endpoints
# ============================================================================

@router.get(
    "/preferences",
    response_model=PreferencesResponse,
    summary="Get platform notification preferences",
    description="The owner's platform-notification event × channel matrix.",
)
async def get_preferences(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    _require_user(auth)
    row = await _get_or_none(db, auth.user_id)
    return _response(row, await _channel_availability(db))


@router.put(
    "/preferences",
    response_model=PreferencesResponse,
    summary="Update platform notification preferences",
    description="Partial update of the matrix and personal contact points.",
)
async def update_preferences(
    request: PreferencesUpdateRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    _require_user(auth)

    row = await _get_or_none(db, auth.user_id)
    if row is None:
        row = UserNotificationPreference(
            user_id=auth.user_id,
            preferences={},
        )
        db.add(row)

    if request.preferences is not None:
        incoming = catalog.sanitize_preferences(request.preferences)
        merged = dict(row.preferences or {})
        for event_key in request.preferences:
            if event_key not in catalog.EVENTS:
                continue
            if incoming.get(event_key):
                merged[event_key] = incoming[event_key]
            else:
                # {} (or all-invalid) resets the event to catalog defaults.
                merged.pop(event_key, None)
        row.preferences = merged

    if request.phone_number is not None:
        phone = request.phone_number.strip()
        if phone and not _PHONE_RE.match(phone):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Phone number must be E.164 format, e.g. +15551234567",
            )
        row.phone_number = phone or None

    if request.pushover_user_key is not None:
        key = request.pushover_user_key.strip()
        row.pushover_user_key = key or None

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to save notification preferences for user {auth.user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save notification preferences",
        )
    await db.refresh(row)

    return _response(row, await _channel_availability(db))
