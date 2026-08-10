"""
Unit tests for services/crawl_targeting — the pure, stdlib-only targeting core
that makes a Dragnet crawl collect "what the operator really wants".

These functions carry the load for goal-directed targeting:
  - score_and_hit / score_url / passes_threshold rank + gate the frontier
    (best-first crawl), and rank_urls is the shared candidate-ranking loop.
  - merge_scope enforces "explicit overrides win over the AI-derived scope".
  - _parse_scope tolerantly parses the derivation model's reply.

They import no app models (the AI client is imported lazily inside
derive_scope), so this module runs standalone — no DB/Redis/AI needed. The one
app dependency is `security.validation` (imported lazily inside _valid_regexes
for the ReDoS gate), whose settings import needs the same local-trial env the
orchestrator loop test sets.
"""
import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Settings refuses to build with the shipped default signing secret; this is a
# throwaway in-process run, so take the documented local-trial escape hatch.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("ALLOW_INSECURE_DEV", "true")

from services import crawl_targeting as ct  # noqa: E402


def test_tokenize_drops_stopwords_and_short_tokens():
    toks = ct.tokenize("Only the API reference and a b cd")
    assert "api" in toks and "reference" in toks
    assert "the" not in toks and "and" not in toks   # stopwords
    assert "a" not in toks and "b" not in toks and "cd" not in toks  # <3 chars


def test_score_ranks_relevant_above_irrelevant():
    t = ct.make_targeting(intent="only the api reference pages",
                          include_paths=[r"^/api/"], max_depth=3)
    s_api = ct.score_url(t, "https://d.co/api/reference/users", "Users API", depth=1)
    s_blog = ct.score_url(t, "https://d.co/blog/hello-world", "Hello world", depth=1)
    assert s_api > s_blog
    # An include-path hit alone is a strong positive signal even without token overlap.
    assert ct.score_url(t, "https://d.co/api/v2/orders", "", depth=1) > 0


def test_include_matches_path_AND_query_not_path_only():
    # A site's content-vs-chrome distinction often lives in the QUERY — /item?id=N
    # vs /news?p=N share a bare path. Include must match path+query so the RIGHT
    # pages are admitted.
    t = ct.make_targeting(intent="", include_paths=[r"^/item\?id=", r"^/news$"], max_depth=3)
    assert ct.include_hit(t, "https://news.ycombinator.com/item?id=123") is True
    assert ct.include_hit(t, "https://news.ycombinator.com/news") is True
    assert ct.include_hit(t, "https://news.ycombinator.com/news?p=2") is False


def test_no_intent_is_shallow_first_reproducing_bfs():
    t = ct.make_targeting(intent="", max_depth=4)
    shallow = ct.score_url(t, "https://d.co/a", depth=0)
    deep = ct.score_url(t, "https://d.co/a/b/c/d", depth=3)
    assert shallow > deep


def test_threshold_keeps_seed_and_include_hits_drops_offtopic():
    t = ct.make_targeting(intent="pricing plans", include_paths=[r"^/pricing"], threshold=0.3)
    # Seed / sitemap / operator picks (depth 0) are never dropped.
    assert ct.passes_threshold(t, 0.0, 0, False) is True
    # An include-path hit is never dropped, whatever its score.
    assert ct.passes_threshold(t, 0.0, 2, True) is True
    # Off-topic + below threshold at depth > 0 → dropped.
    assert ct.passes_threshold(t, 0.05, 2, False) is False
    # A zero threshold disables dropping entirely.
    t0 = ct.make_targeting(intent="pricing", threshold=0.0)
    assert ct.passes_threshold(t0, 0.0, 5, False) is True


def test_merge_scope_explicit_overrides_win():
    derived = {"include_paths": ["^/api/"], "exclude_paths": ["/blog/"], "max_depth": 2}
    m = ct.merge_scope(derived, include_paths=None, exclude_paths=["/x/"], max_depth=None)
    assert m["include_paths"] == ["^/api/"]      # from derived
    assert m["exclude_paths"] == ["/x/"]         # explicit wins
    assert m["max_depth"] == 2                    # from derived
    assert ct.merge_scope(derived, max_depth=7)["max_depth"] == 7


def test_parse_scope_tolerates_fenced_json():
    out = ct._parse_scope(
        'Sure!\n```json\n{"include_paths": ["^/api/", "/docs/"], '
        '"exclude_paths": ["/blog/"], "max_depth": 2, "reason": "API docs"}\n```'
    )
    assert out["include_paths"] == ["^/api/", "/docs/"]
    assert out["exclude_paths"] == ["/blog/"]
    assert out["max_depth"] == 2
    assert out["reason"] == "API docs"


def test_parse_scope_drops_bad_regex_and_clamps_depth():
    out = ct._parse_scope('{"include_paths": ["(unclosed", "/ok/"], "max_depth": 99}')
    assert out["include_paths"] == ["/ok/"]       # invalid regex dropped
    assert out["max_depth"] == 10                  # clamped to 0..10


def test_parse_scope_returns_defaults_on_garbage():
    out = ct._parse_scope("not json at all")
    assert out == {"include_paths": [], "exclude_paths": [], "max_depth": 4, "reason": ""}


def test_score_and_hit_is_the_single_pass_twin():
    """score_and_hit must agree exactly with the (score_url, include_hit) pair it
    replaces on the hot paths — it exists so the include sweep runs once, not to
    change any verdict."""
    t = ct.make_targeting(intent="api reference", include_paths=[r"^/api/"], max_depth=3)
    for url, anchor, depth in [
        ("https://d.co/api/reference/users", "Users API", 1),
        ("https://d.co/blog/hello-world", "Hello world", 2),
        ("https://d.co/api/v2?page=2", "", 0),
        ("https://d.co/pricing%2Fplans", "", 1),
    ]:
        s, h = ct.score_and_hit(t, url, anchor, depth)
        assert s == ct.score_url(t, url, anchor, depth)
        assert h == ct.include_hit(t, url)


def test_short_goal_terms_are_kept_and_match_exactly():
    """Regression: "AI" tokenized to nothing (min length 3), so a /map search for
    it was silently a no-op that returned discovery order. 2-char goal terms now
    carry intent — but only via EXACT token matches (no "ai" ~ "airlines")."""
    t = ct.make_targeting(intent="AI")
    assert t.has_intent, "a 2-char query must not be silently discarded"
    on_topic = ct.score_url(t, "https://d.co/ai/research")
    prefix_bait = ct.score_url(t, "https://d.co/airlines")
    off_topic = ct.score_url(t, "https://d.co/careers")
    assert on_topic > prefix_bait
    assert prefix_bait == off_topic, "short terms get no near-match credit"


def test_near_match_credits_plural_and_stem_variants():
    """Exact-only overlap made rankings look arbitrary whenever the goal's word
    form differed from the URL's (pricing vs /prices, blog vs /blogs). A near
    match earns partial credit — below an exact hit, above a miss."""
    t = ct.make_targeting(intent="pricing")
    exact = ct.score_url(t, "https://d.co/pricing")
    near = ct.score_url(t, "https://d.co/prices")
    miss = ct.score_url(t, "https://d.co/about")
    assert exact > near > miss

    t2 = ct.make_targeting(intent="blog articles")
    assert (ct.score_url(t2, "https://d.co/blogs/article-list")
            > ct.score_url(t2, "https://d.co/contact"))


def test_unmatched_candidates_order_hub_first_not_discovery_order():
    """When the goal matches nothing lexically (opaque URLs, no anchors), the
    zero-signal tail must order hub-first — fewer path segments — instead of
    raw discovery order; shallow nav/section pages are where the links that DO
    carry the goal's words live. Any real match still beats every unmatched
    candidate."""
    t = ct.make_targeting(intent="windows xp")
    shallow = ct.score_url(t, "https://d.co/forums")
    deep = ct.score_url(t, "https://d.co/a/b/c/d/item")
    assert shallow > deep
    assert shallow <= 0 and deep < 0            # both are still zero-signal
    assert ct.score_url(t, "https://d.co/kb/windows-xp-boot") > shallow


def test_rank_urls_returns_top_n_best_first_with_stable_ties():
    t = ct.make_targeting(intent="pricing plans")
    pairs = [
        ("https://d.co/one", ""),             # no overlap
        ("https://d.co/pricing/plans", ""),   # full overlap — best
        ("https://d.co/two", ""),             # no overlap — ties with /one, later
        ("https://d.co/pricing", "plans"),    # anchor text counts too
    ]
    top = asyncio.run(ct.rank_urls(t, pairs, limit=3))
    assert [u for u, _a, _s in top] == [
        "https://d.co/pricing/plans", "https://d.co/pricing", "https://d.co/one"]
    scores = [s for _u, _a, s in top]
    assert scores == sorted(scores, reverse=True)
    # anchor text rode along for the caller's title fallback
    assert top[1][1] == "plans"

    # limit=None → the FULL list, still best-first, ties in discovery order.
    full = asyncio.run(ct.rank_urls(t, pairs))
    assert [u for u, _a, _s in full][-2:] == ["https://d.co/one", "https://d.co/two"]
