"""
Stored-regex guard tests (no DB, no network).

THE INVARIANT under test: a regex the operator STORES — a selector's
``ignore_regex``, a persona's OTP ``code_regex``, a crawl's include/exclude path
patterns — can neither be persisted if it is ReDoS-prone, nor execute without a
hard wall-clock timeout and an input-length cap.

Stored patterns are the dangerous ones. They run unattended, on the event loop,
against attacker-influenced text (a monitored page's content, an inbound email
body, discovered URLs), so a single write of ``(a+)+$`` would otherwise stall the
whole single-worker process — HTTP, agent WebSockets and the scheduler alike,
because CPython holds the GIL through C-level backtracking.

Complements tests/test_redos_guards.py, which covers the extractor and trigger
paths. This file covers the sinks that previously used bare ``re``.
"""
import os
import sys
import time

import pytest

COORDINATOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if COORDINATOR_DIR not in sys.path:
    sys.path.insert(0, COORDINATOR_DIR)

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("API_SECRET_KEY", "test-api-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("HMAC_SECRET_KEY", "test-hmac-secret-0123456789abcdefABCDEF0123456789")
os.environ.setdefault("RECORDER_AUTH_SECRET", "test-recorder-secret-0123456789abcdefABCDEF")

from fastapi import HTTPException  # noqa: E402

from security.validation import InputValidator  # noqa: E402

try:
    import regex as _regex  # noqa: F401
    HAS_REGEX_MODULE = True
except ImportError:  # pragma: no cover - depends on the install
    HAS_REGEX_MODULE = False

# The canonical catastrophic-backtracking pattern, with input that triggers it.
EVIL_PATTERN = r"(a+)+$"
EVIL_INPUT = "a" * 40 + "!"

# A budget generous enough to be stable on a loaded CI box, but far below the
# many-seconds-to-forever an unguarded match would take on EVIL_INPUT.
BUDGET_S = 3.0


# ---------------------------------------------------------------------------
# Layer 1 — the pattern cannot be stored
# ---------------------------------------------------------------------------

def test_validate_regex_rejects_nested_quantifier():
    with pytest.raises(HTTPException):
        InputValidator.validate_regex(EVIL_PATTERN)


def test_validate_regex_accepts_an_ordinary_pattern():
    assert InputValidator.validate_regex(r"\s+ยง\d+") == r"\s+ยง\d+"


def test_crawl_path_patterns_drop_the_unsafe_one_and_keep_the_rest():
    """_valid_regexes is the write-time screen for crawl include/exclude paths."""
    from services.crawl_targeting import _valid_regexes

    kept = _valid_regexes(["^/docs/", EVIL_PATTERN, "(unclosed", "^/blog/"])
    assert kept == ["^/docs/", "^/blog/"]


# ---------------------------------------------------------------------------
# Layer 2 — execution is bounded even when the pattern got through
# ---------------------------------------------------------------------------
# `validate=False` models a row written BEFORE the write-time screen existed:
# the guard must still hold on data already in the database.

@pytest.mark.skipif(
    not HAS_REGEX_MODULE,
    reason="hard timeouts need the `regex` module; without it only the caps apply",
)
def test_safe_regex_sub_is_time_bounded():
    """The selector ignore_regex sink. Unguarded, this does not return."""
    started = time.monotonic()
    result = InputValidator.safe_regex_sub(
        EVIL_PATTERN, "", EVIL_INPUT, validate=False
    )
    assert time.monotonic() - started < BUDGET_S
    # Fail-closed for this sink is "leave the text alone", never "erase it".
    assert result == EVIL_INPUT


@pytest.mark.skipif(
    not HAS_REGEX_MODULE,
    reason="hard timeouts need the `regex` module; without it only the caps apply",
)
def test_safe_regex_search_is_time_bounded():
    """The persona OTP code_regex sink, run against an inbound email body."""
    started = time.monotonic()
    assert InputValidator.safe_regex_search(
        EVIL_PATTERN, EVIL_INPUT, validate=False
    ) is None
    assert time.monotonic() - started < BUDGET_S


def test_safe_regex_sub_caps_its_input_length():
    """Backtracking cost scales with input, so the input is bounded too."""
    oversized = "b" * (InputValidator.MAX_REGEX_INPUT_LENGTH + 5_000)
    out = InputValidator.safe_regex_sub("b", "", oversized, validate=False)
    assert len(out) <= InputValidator.MAX_REGEX_INPUT_LENGTH


# ---------------------------------------------------------------------------
# The helpers must still behave like their `re` counterparts
# ---------------------------------------------------------------------------

def test_safe_regex_sub_substitutes_normally():
    assert InputValidator.safe_regex_sub(r"\d+", "#", "order 123 and 456") == "order # and #"


def test_safe_regex_sub_passes_empty_input_through():
    assert InputValidator.safe_regex_sub(r"\d+", "#", "") == ""


def test_safe_regex_sub_honours_flags():
    import re as _re

    assert InputValidator.safe_regex_sub(
        r"abc", "-", "ABC abc", flags=_re.IGNORECASE
    ) == "- -"
