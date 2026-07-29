"""Custom-path webhooks: `POST /api/v1/webhooks/{custom_path}`.

`WebhookTrigger.custom_path` has always documented this URL, the API-recorder minted
one per function and handed the list back to the caller — and **no route served it**.
Every custom-path URL this coordinator ever produced 404'd. Worse, the recorder created
those triggers with no signing secret, so `_process_webhook`'s fail-closed check meant
they could not be called over `/api/webhooks/hook/{token}` either: the endpoints
reported as created were reachable by no means at all.

What is pinned here:

  * the route exists, and is addressed by the path shape the model advertises;
  * multi-segment paths (`{prefix}/{function}` — what the recorder writes) both
    validate and resolve, and traversal shapes do not;
  * the credential model: a custom path is HUMAN-CHOSEN and guessable, so unlike an
    unguessable token it cannot be its own credential — the route requires a key and
    fails closed without one;
  * a signature stays OPTIONAL on the authenticated route but is still VERIFIED when
    presented (accepting a bad signature because a key was valid would make the header
    meaningless), while the unauthenticated token route keeps demanding one;
  * the callable URL is discoverable from `to_dict()`.
"""
import re

import pytest
from fastapi import HTTPException

from security import api_scopes


# ── the route exists at the advertised shape ───────────────────────────────

def test_custom_path_route_is_registered():
    """The regression itself: the model documented a URL nothing served."""
    from routers.webhooks import custom_path_router

    paths = {(tuple(sorted(r.methods)), r.path) for r in custom_path_router.routes}
    assert (("POST",), "/api/v1/webhooks/{custom_path:path}") in paths


def test_route_is_mounted_without_a_prefix():
    """It is self-prefixed (house convention for /api/v1 — files.py, local_workflows.py).
    Mounting it under `/api` as well would serve `/api/api/v1/...`."""
    main_src = open("main.py").read()
    assert "app.include_router(webhooks_custom_path_router)" in main_src
    assert 'webhooks_custom_path_router, prefix="/api"' not in main_src


# ── path shape: what the recorder writes must be expressible ───────────────

_PATTERN = re.compile(r'^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*$')


@pytest.mark.parametrize("path", ["lookup", "myapp/getUser", "a/b/c", "MyApp/Get_User-2"])
def test_valid_custom_paths(path):
    assert _PATTERN.match(path)


@pytest.mark.parametrize(
    "path",
    [
        "",          # nothing to address
        "/lead",     # stored paths never carry edge slashes; the route strips them
        "lead/",
        "a//b",      # empty segment
        "../etc",    # traversal
        "a.b",       # dots are how traversal gets in
        "my app",
    ],
)
def test_rejected_custom_paths(path):
    assert not _PATTERN.match(path)


def test_create_and_update_models_accept_a_recorder_shaped_path():
    """The old pattern was single-segment, max 64 — it could not express the
    `{prefix}/{function}` paths this coordinator mints itself, so a user could
    neither create nor repair one through the API."""
    from routers.webhooks import WebhookTriggerCreate, WebhookTriggerUpdate

    for model in (WebhookTriggerCreate, WebhookTriggerUpdate):
        field = model.model_fields["custom_path"]
        # Column is String(100); the validator must not be narrower than the store.
        assert field.metadata, f"{model.__name__} lost its custom_path constraints"
        assert any(getattr(m, "max_length", None) == 100 for m in field.metadata)

    created = WebhookTriggerCreate(name="api", custom_path="myapp/getUser")
    assert created.custom_path == "myapp/getUser"

    with pytest.raises(Exception):
        WebhookTriggerCreate(name="api", custom_path="../etc/passwd")


def test_column_length_matches_the_validator():
    """A validator wider than the column would 500 on insert instead of 422."""
    from models.webhook_trigger import WebhookTrigger

    assert WebhookTrigger.__table__.c.custom_path.type.length == 100


# ── the credential model ───────────────────────────────────────────────────

def test_firing_a_custom_path_needs_the_same_scope_as_its_sibling():
    """One vocabulary for one act: firing a webhook is `triggers:execute`, whether
    addressed by trigger id or by custom path."""
    assert api_scopes.required_scope("POST", "/api/v1/webhooks/myapp/getUser") == "triggers:execute"
    assert api_scopes.required_scope("POST", "/api/webhooks/trigger/3") == "triggers:execute"


def test_the_custom_path_route_is_on_the_api_key_surface_at_all():
    """`required_scope` returning None means DENY (fail-closed). A route missing from
    the map cannot be called with a key no matter what scopes it holds — which is how
    a new route silently stays dead."""
    assert api_scopes.required_scope("POST", "/api/v1/webhooks/anything") is not None


def test_a_key_with_only_read_cannot_fire_a_custom_path():
    from security.dependencies import check_api_key_scope

    read_only = {"scopes": api_scopes.build_scopes_blob(["triggers:read"], {})}
    with pytest.raises(HTTPException) as exc:
        check_api_key_scope(read_only, "triggers", "execute")
    assert exc.value.status_code == 403

    can_fire = {"scopes": api_scopes.build_scopes_blob(["triggers:execute"], {})}
    check_api_key_scope(can_fire, "triggers", "execute")  # no raise


# ── signature policy differs per route, on purpose ─────────────────────────

class _FakeTrigger:
    def __init__(self, secret=None, enabled=True):
        self.id = 7
        self.secret = secret
        self.enabled = enabled
        self.workflow_id = 3
        self.conditions = None


class _FakeRequest:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    async def body(self):
        return self._body


@pytest.mark.asyncio
async def test_token_route_still_fails_closed_without_a_secret():
    """The unauthenticated routes are unchanged: the HMAC is the ONLY thing between an
    anonymous caller and the owner's automations, so a secret-less trigger must not run.
    `require_signature` defaults to True precisely so a new unauthenticated route cannot
    accidentally opt out."""
    from routers.webhooks import _process_webhook

    with pytest.raises(HTTPException) as exc:
        await _process_webhook(_FakeTrigger(secret=None), _FakeRequest(), None, None, None)
    assert exc.value.status_code == 401
    assert "signed delivery" in exc.value.detail


@pytest.mark.asyncio
async def test_authenticated_route_does_not_demand_a_signature():
    """On the custom-path route the API key IS the authentication, so a secret-less,
    unsigned request must get PAST the signature gate.

    Asserted as "raises something that is NOT an HTTPException": with these stubs the
    call dies further down on the fake request/db (no `query_params`), which is exactly
    the proof wanted — every 401/403 gate was cleared. If signing were still enforced
    here this would be an HTTPException(401) instead.
    """
    from routers.webhooks import _process_webhook

    with pytest.raises(Exception) as exc:
        await _process_webhook(
            _FakeTrigger(secret=None), _FakeRequest(), None, None, None,
            require_signature=False,
        )
    assert not isinstance(exc.value, HTTPException), (
        f"unsigned authenticated call was rejected at a gate: {exc.value}"
    )


@pytest.mark.asyncio
async def test_a_presented_signature_is_still_verified_when_optional():
    """Optional does not mean ignored: accepting a bad signature because a valid key was
    also presented would make the header decorative."""
    from routers.webhooks import _process_webhook

    with pytest.raises(HTTPException) as exc:
        await _process_webhook(
            _FakeTrigger(secret="plaintext-secret"),
            _FakeRequest(headers={"X-Writ-Timestamp": "1"}),
            None, "sha256=deadbeef", None,
            require_signature=False,
        )
    # Stale timestamp or bad signature — either way, 401. Never a pass.
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_a_signature_with_no_secret_to_check_is_refused():
    """A caller who signs believes something verified them. Nothing did."""
    from routers.webhooks import _process_webhook

    with pytest.raises(HTTPException) as exc:
        await _process_webhook(
            _FakeTrigger(secret=None),
            _FakeRequest(headers={"X-Writ-Timestamp": "1"}),
            None, "sha256=deadbeef", None,
            require_signature=False,
        )
    assert exc.value.status_code == 401
    assert "signing secret" in exc.value.detail


@pytest.mark.asyncio
async def test_a_disabled_trigger_never_runs_on_either_route():
    from routers.webhooks import _process_webhook

    for require in (True, False):
        with pytest.raises(HTTPException) as exc:
            await _process_webhook(
                _FakeTrigger(secret="s", enabled=False), _FakeRequest(), None, None, None,
                require_signature=require,
            )
        assert exc.value.status_code == 403


# ── discoverability ────────────────────────────────────────────────────────

def test_to_dict_exposes_the_callable_custom_url():
    """The path was already in the payload but nothing said what to prefix it with,
    and the two routes authenticate differently — a client could not render the right
    affordance."""
    from models.webhook_trigger import WebhookTrigger

    trigger = WebhookTrigger(name="api", token="tok", custom_path="myapp/getUser")
    d = trigger.to_dict()
    assert d["webhook_path"] == "/api/webhooks/hook/tok"
    assert d["custom_webhook_path"] == "/api/v1/webhooks/myapp/getUser"


def test_to_dict_custom_url_is_none_when_unset():
    """None, not a broken half-URL, so a client can branch on it."""
    from models.webhook_trigger import WebhookTrigger

    assert WebhookTrigger(name="api", token="tok").to_dict()["custom_webhook_path"] is None


# ── the recorder's triggers are now actually callable ──────────────────────

def test_api_recorder_mints_a_signing_secret():
    """Without one, its triggers 401'd on the token route (fail-closed) while the
    custom path had no route — reported as created, callable by nothing."""
    src = open("routers/automation.py").read()
    block = src[src.index("# Auto-create webhook triggers per function"):]
    block = block[: block.index("await db.commit()")]
    assert "SecretEncryption.encrypt_secret" in block
    assert "token_urlsafe" in block


def test_api_recorder_prefix_cannot_produce_an_unmatchable_path():
    """An all-punctuation name sanitized to "" would build "/getUser" — a leading
    slash no stored path ever has, so the endpoint could never be resolved."""
    src = open("routers/automation.py").read()
    block = src[src.index("# Auto-create webhook triggers per function"):]
    block = block[: block.index("await db.commit()")]
    assert "if not prefix:" in block


def test_api_recorder_returns_the_endpoints_it_minted():
    """`created_triggers` was assembled and then DISCARDED — the caller asked for
    callable functions and got back no way to call them. The field is additive, so
    clients reading only the plain workflow fields are unaffected."""
    from routers.automation import ApiRecordedWorkflowResponse, WorkflowResponse

    assert issubclass(ApiRecordedWorkflowResponse, WorkflowResponse)
    field = ApiRecordedWorkflowResponse.model_fields["endpoints"]
    assert not field.is_required(), "endpoints must be optional to stay backward compatible"


def test_api_recorder_endpoint_records_carry_a_callable_url():
    """A bare `custom_path` fragment is not actionable: nothing tells the caller what
    to prefix it with, and the two webhook routes authenticate differently."""
    src = open("routers/automation.py").read()
    block = src[src.index("# Auto-create webhook triggers per function"):]
    block = block[: block.index("await db.commit()")]
    assert '"url": f"/api/v1/webhooks/{custom_path}"' in block
    assert '"method": "POST"' in block
