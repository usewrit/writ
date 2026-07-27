"""API key scope vocabulary + fail-closed route gate.

These cover the three failures the old per-resource CRUD matrix had:
  1. surfaces with no representation at all (vault, crawl, billing) were reachable
     by any key, because the modern auth path applied no path check;
  2. permissions routers demanded but no key could hold (`workflows:run`,
     `workflows:execute`, the whole `files` resource) — so triggering a run with
     an API key was impossible;
  3. each app offered a different subset of the vocabulary.
"""
import pytest
from fastapi import HTTPException

from security import api_scopes


# ── vocabulary integrity ───────────────────────────────────────────────────

def test_every_route_scope_is_grantable():
    """No route may demand a scope the vocabulary cannot express.

    Import-time guarded too, but asserted here so the failure names the module.
    """
    demanded = {scope for _, _, scope in api_scopes._ROUTE_RULES}
    assert not [s for s in demanded if not api_scopes.is_valid_scope(s)]


def test_every_grantable_scope_is_gated():
    """No scope may be offered in the UI that no route ever checks."""
    assert set(api_scopes.all_scopes()) - {s for _, _, s in api_scopes._ROUTE_RULES} == set()


def test_running_a_workflow_is_expressible():
    """The regression that motivated the rewrite: `workflows:run` was demanded by
    automation.py but `run` was not a permission any key could be granted, so the
    check could never pass."""
    blob = api_scopes.build_scopes_blob(["workflows:execute"], {})
    assert api_scopes.check(blob, "workflows", "run")      # legacy spelling
    assert api_scopes.check(blob, "workflows", "execute")
    assert api_scopes.required_scope("POST", "/api/automation/workflows/12/run") == "workflows:execute"


def test_files_resource_is_grantable():
    """files:* was enforced at five call sites but absent from RESOURCE_TYPES, so
    a key carrying it was rejected at creation and those routes were dead."""
    blob = api_scopes.build_scopes_blob(["files:read", "files:write"], {})
    assert api_scopes.check(blob, "files", "read")
    assert api_scopes.required_scope("POST", "/api/v1/files") == "files:write"


# ── grant normalization ────────────────────────────────────────────────────

def test_wildcard_is_expanded_at_grant_time():
    """Stored grants are concrete, so adding an action to a resource later cannot
    silently widen a key minted today."""
    blob = api_scopes.build_scopes_blob(["runs:*"], {})
    assert blob["scopes"] == ["runs:read"]
    assert "runs:*" not in blob["scopes"]


def test_invalid_scopes_are_dropped_not_stored():
    blob = api_scopes.build_scopes_blob(["workflows:read", "nonsense:read", "workflows:teleport"], {})
    assert blob["scopes"] == ["workflows:read"]


def test_pins_require_a_scope_on_that_resource():
    """A pin on a resource the key holds nothing for reads as a restriction on the
    detail screen while granting nothing — so it is not stored."""
    blob = api_scopes.build_scopes_blob(["workflows:read"], {"workflows": [1], "monitors": [2]})
    assert blob["ids"] == {"workflows": [1]}


def test_pins_are_ignored_for_unpinnable_resources():
    blob = api_scopes.build_scopes_blob(["crawl:execute"], {"crawl": [1]})
    assert blob["ids"] == {}


def test_object_pinning_is_enforced():
    blob = api_scopes.build_scopes_blob(["workflows:read"], {"workflows": [12, 15]})
    assert api_scopes.check(blob, "workflows", "read", 12)
    assert not api_scopes.check(blob, "workflows", "read", 99)


def test_legacy_resource_names_map_to_one_surface():
    """`checks`, `targets` and `automations` were three names for two surfaces."""
    blob = api_scopes.build_scopes_blob(["monitors:read", "workflows:read"], {})
    assert api_scopes.check(blob, "checks", "read")
    assert api_scopes.check(blob, "targets", "read")
    assert api_scopes.check(blob, "automations", "read")


# ── presets ────────────────────────────────────────────────────────────────

def test_read_only_preset_grants_no_mutations():
    scopes = set(api_scopes.preset_scopes("read_only"))
    assert not [s for s in scopes if not s.endswith(":read")]


def test_run_preset_can_execute_but_not_delete():
    scopes = set(api_scopes.preset_scopes("run"))
    assert "workflows:execute" in scopes
    assert "crawl:execute" in scopes
    assert not [s for s in scopes if s.endswith(":delete") or s.endswith(":write")]


def test_presets_round_trip_through_match():
    for preset in ("read_only", "run", "full"):
        assert api_scopes.match_preset(api_scopes.preset_scopes(preset)) == preset


def test_catalog_actions_match_declared_resources():
    """The catalogue is what the three UIs render; it must not offer an action the
    resource does not declare."""
    catalog = api_scopes.catalog()
    for resource in catalog["resources"]:
        for action in resource["actions"]:
            assert api_scopes.is_valid_scope(f"{resource['key']}:{action}")


# ── fail-closed route gate ─────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/api/automation/workflows/12/run", "workflows:execute"),
    ("GET",  "/api/automation/workflows",        "workflows:read"),
    ("DELETE", "/api/automation/workflows/3",    "workflows:delete"),
    ("GET",  "/api/automation/workflows/3/data", "datasets:read"),
    ("POST", "/api/crawl",                       "crawl:execute"),
    ("POST", "/api/crawl/scrape",                "scrape:execute"),
    ("GET",  "/api/vault/secrets",               "secrets:read"),
    ("POST", "/api/targets/9/run",               "monitors:execute"),
    ("POST", "/mcp",                             "mcp:execute"),
])
def test_route_map_resolves(method, path, expected):
    assert api_scopes.required_scope(method, path) == expected


@pytest.mark.parametrize("method,path", [
    # Not part of the API-key surface at all.
    ("POST", "/api/auth/api-keys"),          # a key must never mint another key
    ("GET",  "/api/auth/api-keys"),
    # The agent daemon's own enrolment protocol — a user key must not be able to
    # speak it and impersonate a device.
    ("POST", "/api/agents/register"),
    ("POST", "/api/agents/poll"),
    ("POST", "/api/agents/heartbeat"),
    ("POST", "/api/agents/deregister"),
])
def test_unmapped_paths_are_denied(method, path):
    assert api_scopes.required_scope(method, path) is None


def test_scrape_does_not_authorise_a_full_crawl():
    """Starting a site-wide crawl is materially more expensive than one scrape, so
    they are separate resources rather than two verbs on one."""
    blob = api_scopes.build_scopes_blob(["scrape:execute"], {})
    granted = api_scopes.granted_scopes(blob)
    assert api_scopes.required_scope("POST", "/api/crawl/scrape") in granted
    assert api_scopes.required_scope("POST", "/api/crawl") not in granted


@pytest.mark.anyio
async def test_route_gate_refuses_unmapped_and_ungranted():
    from types import SimpleNamespace
    from models.api_key import Role
    from security.dependencies import _enforce_key_route_scope

    def request(method, path):
        return SimpleNamespace(method=method, url=SimpleNamespace(path=path))

    key = SimpleNamespace(
        id=7,
        label="ci-runner",
        role=Role.CLIENT,
        scopes=api_scopes.build_scopes_blob(["workflows:read"], {}),
    )

    # Granted → passes.
    await _enforce_key_route_scope(request("GET", "/api/automation/workflows"), key)

    # Mapped but not granted → 403 naming the missing scope.
    with pytest.raises(HTTPException) as exc:
        await _enforce_key_route_scope(request("POST", "/api/automation/workflows/1/run"), key)
    assert exc.value.status_code == 403
    assert "workflows:execute" in exc.value.detail

    # Unmapped → 403, NOT a pass-through. This is the whole point: the old modern
    # auth path returned an AuthContext here with no check at all.
    for method, path in (("GET", "/api/vault/secrets"), ("GET", "/api/auth/api-keys")):
        with pytest.raises(HTTPException) as exc:
            await _enforce_key_route_scope(request(method, path), key)
        assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_admin_key_bypasses_the_route_map():
    """The bootstrap admin key (scripts/bootstrap_admin.py) exists to administer
    the coordinator, so it is not held to the route map."""
    from types import SimpleNamespace
    from models.api_key import Role
    from security.dependencies import _enforce_key_route_scope

    key = SimpleNamespace(id=8, label="platform-ops", role=Role.ADMIN, scopes={})
    await _enforce_key_route_scope(
        SimpleNamespace(method="GET", url=SimpleNamespace(path="/api/settings")), key
    )


def test_dataset_delete_is_expressible_and_separate_from_workflow_delete():
    """Clearing a dataset removes extracted RECORDS; the workflow survives. The two
    must not share a scope, or "let this script prune old records" would also
    authorise deleting the automation that produces them."""
    blob = api_scopes.build_scopes_blob(["datasets:read", "datasets:delete"], {})
    granted = api_scopes.granted_scopes(blob)
    assert api_scopes.required_scope("DELETE", "/api/automation/workflows/9/data") in granted
    # ...but not the workflow itself.
    assert api_scopes.required_scope("DELETE", "/api/automation/workflows/9") not in granted


def test_read_only_dataset_key_cannot_delete():
    blob = api_scopes.build_scopes_blob(["datasets:read"], {})
    assert not api_scopes.check(blob, "datasets", "delete")
