"""Self-host must carry the same persona-session guards as the cloud edition.

These are behaviour-parity checks, deliberately source-level: the two editions are
hand-mirrored (not generated), so a cloud fix silently failing to land here is the
realistic failure mode — and it is invisible until a self-host user's crawl quietly
returns signed-out pages.

The defects being guarded, all found in production:
  * a stale session restored before a sign-in poisons its CSRF token, so the login
    silently fails and the persona banks an ANONYMOUS jar;
  * a probe against a PUBLIC url always answers "signed in", so verification that
    targets the crawl seed proves nothing;
  * banking a jar on `success` alone overwrites a good session and stamps the
    persona "valid", manufacturing the problem above;
  * treating "no login workflow" as a dead end, when credentials alone are enough.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "services" / "persona_session_probe.py"
LOGIN = (ROOT / "services" / "persona_login.py").read_text()
SEEDER = (ROOT / "services" / "crawl_orchestrator.py").read_text()
AUTOMATION = (ROOT / "routers" / "automation.py").read_text()
QUEUE = (ROOT / "services" / "workflow_queue.py").read_text()


def test_the_probe_module_exists():
    # The seeder CALLS gated_url_for_persona; without the module that call raised
    # and the guard failed open — verification silently absent, crawls unprotected.
    assert PROBE.exists(), "services/persona_session_probe.py is missing"
    assert "def gated_url_for_persona" in PROBE.read_text()


def test_the_seeder_probes_a_gated_url_not_the_seed():
    assert "gated_url_for_persona" in SEEDER
    # The crawl seed is usually public; probing it is worthless.
    assert "probe_url" in SEEDER


def test_a_session_is_verified_before_it_is_reused():
    assert "_session_is_signed_out" in LOGIN
    i = LOGIN.index("if not force:")
    assert "_session_is_signed_out" in LOGIN[i:i + 1200]


def test_credentials_alone_can_sign_a_persona_in():
    assert "def has_login_credentials(" in LOGIN
    assert "_bootstrap_login_via_ai" in LOGIN
    assert "start_login_record_session" in LOGIN
    # Every gate that used to dead-end on a missing workflow.
    assert "has_login_credentials(persona)" in SEEDER
    assert "_has_creds(_persona)" in SEEDER


def test_login_runs_start_cold_on_both_dispatch_lanes():
    # Inline dispatch AND the queue: at the concurrency cap the login lands in the
    # queue, where restoring the stale session reproduces the original bug.
    assert '_persona_login' in AUTOMATION
    assert '_persona_login' in QUEUE
    i = QUEUE.index('_persona_login')
    assert "session_state = None" in QUEUE[i:i + 300]


def test_a_harvested_jar_is_only_banked_when_it_proves_a_login():
    assert "session_has_auth_material" in AUTOMATION
    assert AUTOMATION.index("session_has_auth_material") < AUTOMATION.rindex(
        "await PersonaService.save_session"
    )
    streaming = (ROOT / "services" / "streaming_manager.py").read_text()
    assert "session_has_auth_material" in streaming


def test_every_session_lane_routes_through_the_verifier():
    lanes = {
        "streaming": ROOT / "routers" / "streaming.py",
        "targets": ROOT / "routers" / "targets.py",
        "frontier mapping": ROOT / "routers" / "crawl.py",
    }
    missing = [n for n, p in lanes.items() if "ensure_fresh_session" not in p.read_text()]
    assert not missing, f"lanes handing over an unverified session: {missing}"
