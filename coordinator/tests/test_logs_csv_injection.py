"""
CSV/formula-injection guard for the audit-log export (routers/logs.py).

THE INVARIANT under test: every free-text cell written to the exported CSV
(actor, action, message) that begins with a formula-trigger character
(=, +, -, @, leading tab/CR) is prefixed with a single quote, so opening the
export in Excel/Sheets renders it as literal text instead of executing it as a
formula (e.g. ``=cmd|'/c calc'!A1``).

Pure unit test — imports only the sanitizer, no DB / no app. Runnable with plain
`python3 coordinator/tests/test_logs_csv_injection.py`.
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from routers.logs import _csv_safe  # noqa: E402


def test_formula_triggers_are_neutralized():
    for payload in ("=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1)", "\tx", "\rx"):
        out = _csv_safe(payload)
        assert out.startswith("'"), f"{payload!r} not neutralized -> {out!r}"
        assert out == "'" + payload


def test_benign_values_unchanged():
    for payload in ("login", "user@example.com is not at the start", "1.2.3.4", ""):
        assert _csv_safe(payload) == payload


def test_none_becomes_empty_string():
    assert _csv_safe(None) == ""


def test_non_string_is_stringified_then_checked():
    # A non-string that stringifies to a benign value passes through.
    assert _csv_safe(42) == "42"


if __name__ == "__main__":  # script-style fallback (no pytest required)
    test_formula_triggers_are_neutralized()
    test_benign_values_unchanged()
    test_none_becomes_empty_string()
    test_non_string_is_stringified_then_checked()
    print("test_logs_csv_injection: OK")
