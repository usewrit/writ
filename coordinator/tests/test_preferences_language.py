"""UI-language coercion for the owner preferences section.

The invariant these guard: "never picked a language" (None) must stay
distinguishable from "explicitly picked English". The web UI applies the stored
preference over the browser's own language, so collapsing an unset value to "en"
would force English on a French/Spanish browser that never chose anything.
"""

import pytest

from services.coordinator_settings import (
    PREFERENCES_DEFAULTS,
    SUPPORTED_LANGUAGES,
    coerce_language,
)


def test_default_language_is_unset_not_english():
    assert PREFERENCES_DEFAULTS["language"] is None


@pytest.mark.parametrize("code", SUPPORTED_LANGUAGES)
def test_supported_codes_pass_through(code):
    assert coerce_language(code) == code


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "de", "zh-Hans", "klingon", 0, False, [], {}],
)
def test_unset_and_unsupported_collapse_to_none(value):
    assert coerce_language(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("fr-CA", "fr"), ("es-MX", "es"), ("EN", "en"), ("  Fr  ", "fr"), ("en-GB", "en")],
)
def test_region_tags_and_case_fold_to_base(value, expected):
    assert coerce_language(value) == expected


def test_explicit_english_is_preserved():
    """An explicit "en" must survive, so it can override a non-English browser."""
    assert coerce_language("en") == "en"
    assert coerce_language("en") is not None
