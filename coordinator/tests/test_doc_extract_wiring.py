"""The document/OCR extraction lane must arrive on an agent by itself.

The agent reads DOC_EXTRACT_URL from its own environment and treats an unset
value as "silently skip every non-HTML resource". That default is safe but
invisible: an install with no wiring looks perfectly healthy while dropping
every PDF a crawl reaches. So the coordinator hands the settings to the agent as
part of connecting it, and these tests hold that contract in place.

They also pin the container-vs-host address rewrite. A loopback URL is correct
from the host and wrong from inside a container, where 127.0.0.1 is the
container itself — a failure mode that looks like a network problem rather than
a configuration one.
"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import settings  # noqa: E402
from routers import fleet  # noqa: E402


@pytest.fixture
def doc_settings(monkeypatch):
    """Point the coordinator at a bundled extractor on its own loopback."""
    monkeypatch.setattr(settings, "doc_extract_url", "http://127.0.0.1:8092", raising=False)
    monkeypatch.setattr(settings, "doc_extract_secret", "s3cret-value", raising=False)
    return settings


def test_env_carries_url_and_secret(doc_settings):
    env = fleet._doc_extract_env()
    assert env == {
        "DOC_EXTRACT_URL": "http://127.0.0.1:8092",
        "DOC_EXTRACT_SECRET": "s3cret-value",
    }


def test_env_is_empty_when_lane_disabled(monkeypatch):
    """An operator who blanks the URL gets clean commands, not dead variables."""
    monkeypatch.setattr(settings, "doc_extract_url", "", raising=False)
    monkeypatch.setattr(settings, "doc_extract_secret", "s3cret-value", raising=False)
    assert fleet._doc_extract_env() == {}


def test_url_without_secret_still_enables_the_lane(monkeypatch):
    """The secret is optional; the service itself decides whether to require it."""
    monkeypatch.setattr(settings, "doc_extract_url", "https://docs.example.com", raising=False)
    monkeypatch.setattr(settings, "doc_extract_secret", None, raising=False)
    assert fleet._doc_extract_env() == {"DOC_EXTRACT_URL": "https://docs.example.com"}


def test_trailing_slash_is_normalised(monkeypatch):
    monkeypatch.setattr(settings, "doc_extract_url", "https://docs.example.com/", raising=False)
    monkeypatch.setattr(settings, "doc_extract_secret", "", raising=False)
    assert fleet._doc_extract_env()["DOC_EXTRACT_URL"] == "https://docs.example.com"


def test_binary_command_carries_the_lane(doc_settings, monkeypatch):
    monkeypatch.setattr(settings, "writ_public_url", "https://writ.example.com", raising=False)
    cmds = fleet._build_connect_commands("tok-123")
    binary = cmds["connect_command"]

    assert "WRIT_SERVICE_TOKEN=tok-123" in binary
    assert "DOC_EXTRACT_URL=http://127.0.0.1:8092" in binary
    assert "DOC_EXTRACT_SECRET=s3cret-value" in binary
    # The env must precede the binary, or the shell treats it as an argument.
    assert binary.index("DOC_EXTRACT_URL") < binary.index("./writ-agent start")


def test_docker_command_rewrites_loopback_for_the_container(doc_settings, monkeypatch):
    """127.0.0.1 inside a container is the container — both URLs must be rewritten."""
    monkeypatch.setattr(settings, "writ_public_url", "http://localhost:8000", raising=False)
    docker = fleet._build_connect_commands("tok-123")["docker_command"]

    assert "DOC_EXTRACT_URL=http://host.docker.internal:8092" in docker
    assert "saas.url http://host.docker.internal:8000" in docker
    # Linux Docker does not provide the alias for free.
    assert "--add-host host.docker.internal:host-gateway" in docker
    # host.docker.internal is not loopback, so the agent now needs the plaintext
    # opt-in it did not need from the host.
    assert "saas.allow_insecure true" in docker


def test_docker_command_leaves_routable_urls_alone(monkeypatch):
    monkeypatch.setattr(settings, "writ_public_url", "https://writ.example.com", raising=False)
    monkeypatch.setattr(settings, "doc_extract_url", "https://docs.example.com", raising=False)
    monkeypatch.setattr(settings, "doc_extract_secret", "s3cret-value", raising=False)
    docker = fleet._build_connect_commands("tok-123")["docker_command"]

    assert "host.docker.internal" not in docker
    assert "--add-host" not in docker
    assert "DOC_EXTRACT_URL=https://docs.example.com" in docker
    # https needs no plaintext opt-in.
    assert "allow_insecure" not in docker


def test_docker_command_has_no_doc_flags_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "writ_public_url", "https://writ.example.com", raising=False)
    monkeypatch.setattr(settings, "doc_extract_url", "", raising=False)
    cmds = fleet._build_connect_commands("tok-123")

    assert "DOC_EXTRACT" not in cmds["docker_command"]
    assert "DOC_EXTRACT" not in cmds["connect_command"]


def test_local_agent_inherits_the_lane(doc_settings):
    """The one-click 'run an agent on this host' path uses the same source.

    It imports _doc_extract_env directly rather than rebuilding the mapping, so
    the two paths cannot drift apart. Assert the import still resolves.
    """
    from services import local_agent

    src = Path(local_agent.__file__).read_text()
    assert "_doc_extract_env" in src, (
        "local_agent no longer passes the document/OCR settings to the agent it "
        "starts — a one-click agent would come up with the lane dark."
    )
