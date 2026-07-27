"""
ReDoS guard unit tests (no DB, no network).

THE INVARIANT under test: user-supplied regex patterns that reach the extractor
service and the trigger 'matches' operator cannot (a) be stored if they are
ReDoS-prone, nor (b) execute without a hard wall-clock timeout + input-length
cap. These two layers together close the stored-ReDoS vector even when the
optional third-party ``regex`` (hard-timeout) module is not installed — in that
case enforcement relies on InputValidator.validate_regex's length/nesting/
structural caps, which the rejection tests below exercise directly.

Runnable with plain ``python3 coordinator/tests/test_redos_guards.py``.
"""
import os
import sys
import time

try:  # pytest drives this under the suite; script-style runs without it.
    import pytest
except ModuleNotFoundError:  # pragma: no cover - script-style fallback
    pytest = None

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from fastapi import HTTPException  # noqa: E402

from security.validation import InputValidator  # noqa: E402
from services.extractor import extractor_service  # noqa: E402


# --- pattern validation -----------------------------------------------------

def test_validate_regex_accepts_simple_pattern():
    assert InputValidator.validate_regex(r"price:\s*\$(\d+)") == r"price:\s*\$(\d+)"


def test_validate_regex_rejects_nested_quantifier_bomb():
    # Classic (a+)+ catastrophic-backtracking construct must be rejected by the
    # structural heuristic regardless of whether `regex` is installed.
    try:
        InputValidator.validate_regex(r"(a+)+$")
        raised = False
    except HTTPException as e:
        raised = True
        assert e.status_code == 400
    assert raised, "expected (a+)+ to be rejected"


def test_validate_regex_rejects_alternation_bomb():
    try:
        InputValidator.validate_regex(r"(a|a)+$")
        raised = False
    except HTTPException as e:
        raised = True
        assert e.status_code == 400
    assert raised, "expected (a|a)+ to be rejected"


def test_validate_regex_rejects_overlong_pattern():
    try:
        InputValidator.validate_regex("a" * (InputValidator.MAX_REGEX_LENGTH + 1))
        raised = False
    except HTTPException as e:
        raised = True
        assert e.status_code == 400
    assert raised, "expected overlong pattern to be rejected"


# --- safe execution ---------------------------------------------------------

def test_safe_regex_search_matches_and_caps_input():
    m = InputValidator.safe_regex_search(r"(\d+)", "order 4242 placed")
    assert m is not None and m.group(1) == "4242"

    # Input is capped: anything past MAX_REGEX_INPUT_LENGTH is dropped before
    # matching, so a needle past the cap is not found.
    cap = InputValidator.MAX_REGEX_INPUT_LENGTH
    haystack = ("." * (cap + 50)) + "NEEDLE"
    assert InputValidator.safe_regex_search("NEEDLE", haystack) is None


def test_safe_regex_findall_returns_all():
    assert InputValidator.safe_regex_findall(r"\d+", "a1 b22 c333") == ["1", "22", "333"]


def test_extractor_regex_runs_under_guard():
    extracted = extractor_service.extract_all(
        content="SKU: ABC-123 in stock",
        extractors=[{
            "name": "sku",
            "output_name": "sku",
            "enabled": True,
            "extract_type": "regex",
            "config": {"pattern": r"SKU:\s*([A-Z]+-\d+)", "group": 1},
            "is_array": False,
        }],
        content_type="text",
    )
    assert extracted["sku"] == "ABC-123"


def test_extractor_with_redos_pattern_fails_closed_fast():
    # A catastrophic pattern persisted before validation existed must not hang
    # the extractor: validate_regex (called inside safe_regex_*) rejects it, the
    # extractor swallows the HTTPException, and the default_value is returned.
    start = time.monotonic()
    extracted = extractor_service.extract_all(
        content=("a" * 64) + "!",
        extractors=[{
            "name": "bomb",
            "output_name": "bomb",
            "enabled": True,
            "extract_type": "regex",
            "config": {"pattern": r"(a+)+$"},
            "is_array": False,
            "default_value": "DEFAULT",
        }],
        content_type="text",
    )
    elapsed = time.monotonic() - start
    assert extracted["bomb"] == "DEFAULT"
    # Must return promptly (validation rejects pre-execution); generous bound.
    assert elapsed < 2.0, f"extractor took too long ({elapsed:.2f}s) on ReDoS pattern"


if __name__ == "__main__":  # pragma: no cover - script-style runner
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
