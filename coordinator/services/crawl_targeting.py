"""
Crawl targeting — turn a plain-English crawl GOAL into concrete scope + a cheap
relevance score, so a Dragnet crawl collects "what the operator really wants"
instead of a blind breadth-first sweep.

Two pieces, both dependency-light (stdlib only) so they are reusable across the
frontier hot path (`crawl_orchestrator._admit`), the `/crawl/preview` +
`/crawl/map` endpoints, and the fleet agent later:

  1. derive_scope(...) — a ONE-SHOT AI call that reads the goal + a sample of the
     site's real URLs and returns include/exclude path regexes + a max_depth.
     This is the "say what you want in English" translation. It is best-effort
     and NEVER raises — on any failure (including no AI configured at all, the
     common self-host case) it returns whole-site defaults.

  2. score_and_hit(...) / score_url(...) — a lexical relevance score in
     ~[-0.25, 1.0] used to order the frontier best-first. No AI, no network, no
     embeddings: token overlap of the goal against the URL path + anchor text
     (exact, plus a discounted credit for near-matches like plural/stem variants),
     plus a boost for include-path hits, minus a mild depth penalty. With NO goal
     it reduces to shallow-first, which reproduces the plain BFS sweep (the safe
     default for un-targeted crawls).

  3. rank_urls(...) — the one shared ranking loop over a candidate list. Bounded
     heap (top-N without sorting the world) and it yields the event loop
     periodically, because candidate lists are SITE-controlled: an 8 MB sitemap
     is ~100k URLs, and scoring those in one sync sweep inside an async handler
     pins every other request on the coordinator.

The scorer is intentionally deterministic and side-effect-free so it can run on
the dispatch hot path without a network call or per-URL model inference.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, unquote

logger = logging.getLogger(__name__)

# Common English + URL-boilerplate terms that carry no targeting signal. Kept
# small on purpose — the goal is to drop noise, not to do real stemming. The
# 2-char entries matter because GOAL text keeps 2-char tokens (see tokenize):
# "ai"/"3d"/"tv" are real intent, "of"/"to"/"in" are not.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "all",
    "any", "you", "your", "our", "get", "only", "just", "about", "into", "per",
    "via", "some", "page", "pages", "site", "website", "url", "urls", "http",
    "https", "www", "com", "give", "want", "need", "find", "show", "every",
    "each", "its", "their", "them", "they", "have", "has", "will", "can",
    "of", "to", "in", "on", "at", "by", "an", "as", "is", "it", "be", "we",
    "or", "if", "so", "do", "no", "me", "my", "up", "us",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str, min_len: int = 3) -> set:
    """Lowercased alphanumeric tokens ≥ ``min_len`` chars, minus stopwords.

    Goal text uses ``min_len=2`` so short-but-meaningful query terms ("ai",
    "3d", "ev") still carry intent instead of being silently dropped — which
    made a /map search for "AI" a no-op that returned discovery order."""
    if not text:
        return set()
    return {t for t in _TOKEN_RE.findall(text.lower())
            if len(t) >= min_len and t not in _STOPWORDS}


def _compile(patterns) -> tuple:
    """Pre-compile include-path regexes, silently dropping invalid ones."""
    out = []
    for p in patterns or []:
        if not isinstance(p, str) or not p.strip():
            continue
        try:
            out.append(re.compile(p))
        except re.error:
            continue
    return tuple(out)


@dataclass(frozen=True)
class Targeting:
    """Pre-computed targeting state for one crawl. Build once, reuse per URL."""
    tokens: frozenset          # goal terms
    include_res: tuple         # compiled include_paths patterns (for the boost)
    max_depth: int
    threshold: float           # drop-below relevance (0.0 = disabled)
    # First-2-chars of every goal token — the cheap gate that keeps the
    # near-match pass O(few) per URL instead of goal×candidate.
    prefix2: frozenset = field(default_factory=frozenset)

    @property
    def has_intent(self) -> bool:
        return bool(self.tokens)


def make_targeting(*, intent: str = "", extract_prompt: str = "",
                   include_paths=None, max_depth: int = 4,
                   threshold: float = 0.0) -> Targeting:
    """Build a Targeting from raw values (used by /preview and /map, which have
    no CrawlJob row).

    ``extract_prompt`` is accepted for signature parity with the shared contract
    but is always empty here — the coordinator runs deterministic crawls only, so
    there is no per-page AI instruction to mine tokens from."""
    try:
        md = int(max_depth) if max_depth is not None else 4
    except Exception:
        md = 4
    try:
        th = float(threshold or 0.0)
    except Exception:
        th = 0.0
    tokens = frozenset(tokenize(intent, 2) | tokenize(extract_prompt, 2))
    return Targeting(
        tokens=tokens,
        include_res=_compile(include_paths),
        max_depth=md,
        threshold=th,
        prefix2=frozenset(t[:2] for t in tokens),
    )


def build_targeting(crawl) -> Targeting:
    """Build a Targeting from a CrawlJob row (used by the frontier hot path)."""
    return make_targeting(
        intent=getattr(crawl, "intent", "") or "",
        include_paths=getattr(crawl, "include_paths", None),
        max_depth=getattr(crawl, "max_depth", 4),
        threshold=getattr(crawl, "relevance_threshold", 0.0),
    )


def _path_query(url: str) -> str:
    """Path + `?query` — the target for include-pattern matching. The query is included because a
    site's content-vs-chrome distinction often lives in the query (HN `/item?id=` story vs `/news?p=2`
    pagination share a bare path), so a path-only match can't tell them apart."""
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


def include_hit(t: Targeting, url: str) -> bool:
    """Does this URL's path+query match any include pattern? (A strong relevance signal
    even when the goal tokens don't overlap the URL text.)

    Callers that also need the score must use `score_and_hit` — calling this AND
    `score_url` runs the include sweep (and urlsplit) twice per URL."""
    return any(rx.search(_path_query(url)) for rx in t.include_res)


# Credit for a near-match (plural/stem variant) relative to an exact token hit.
_NEAR_WEIGHT = 0.7

# Tie-break for candidates the goal does NOT match at all: prefer hub-like pages
# (fewer path segments). When a goal matches nothing lexically — opaque URLs,
# sitemap rows with no anchor text — every candidate used to tie at ~0 and the
# "ranking" was raw discovery order. Shallow pages are where the links that DO
# carry the goal's words live (nav, section indexes), so they are the right
# thing to surface in /map and to explore first in a crawl. Small on purpose:
# max 0.048, so it can never outweigh any real token or include-path signal.
_HUB_PRIOR = 0.008
_HUB_PRIOR_MAX_SEGS = 6


def _near_match(g: str, c: str) -> bool:
    """Cheap morphological credit, no stemmer: one token is a prefix of the other
    ("api"/"apis", "blog"/"blogger"), or two ≥5-char tokens share their first 4
    chars ("pricing"/"prices", "article"/"articles"). Exact-only matching made
    rankings look arbitrary the moment the goal's word form differed from the
    URL's — the most common way a /map search "seemed broken"."""
    shorter = len(g) if len(g) < len(c) else len(c)
    if shorter >= 3 and (c.startswith(g) or g.startswith(c)):
        return True
    return len(g) >= 5 and len(c) >= 5 and g[:4] == c[:4]


def score_and_hit(t: Targeting, url: str, anchor_text: str = "",
                  depth: int = 0) -> tuple:
    """`score_url` + `include_hit` in ONE pass — urlsplit and the include-pattern
    sweep run once, not once per predicate. Every hot path (frontier admission,
    /preview, /map) needs both, and the two-call shape doubled the expensive
    half of scoring for each candidate URL."""
    parts = urlsplit(url)
    pq = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    hit = any(rx.search(pq) for rx in t.include_res)
    ih = 1.0 if hit else 0.0
    # Depth penalty scaled by the crawl's own max_depth so shallower pages sort
    # first without ever flipping a strong content match below a shallow miss.
    depth_pen = 0.25 * max(0, depth) / (t.max_depth + 1)
    if not t.tokens:
        return 0.45 * ih - depth_pen, hit
    hay = unquote(pq)
    if anchor_text:
        hay = f"{hay} {anchor_text}"
    # Raw tokens on the candidate side — no length/stopword filter. _TOKEN_RE
    # already splits on every separator, and the goal side is filtered, so the
    # intersection can't pick up noise; filtering here only cost time and made
    # 2-char goal terms unmatchable.
    cand = set(_TOKEN_RE.findall(hay.lower()))
    exact = t.tokens & cand
    matched = float(len(exact))
    if len(exact) < len(t.tokens):
        # Near-matches only for goal tokens that missed, only against candidate
        # tokens sharing a first-2-chars bucket (both _near_match shapes imply a
        # shared 2-char prefix), so this stays O(few) per URL.
        near = [c for c in cand if c[:2] in t.prefix2]
        if near:
            for g in t.tokens:
                if g not in exact and any(_near_match(g, c) for c in near):
                    matched += _NEAR_WEIGHT
    if not matched:
        # Nothing overlapped: order this zero-signal tail hub-first instead of
        # by discovery order. Matched candidates are never touched by this.
        p = (parts.path or "/").strip("/")
        segs = (p.count("/") + 1) if p else 0
        return 0.45 * ih - depth_pen - _HUB_PRIOR * min(segs, _HUB_PRIOR_MAX_SEGS), hit
    overlap = matched / len(t.tokens)
    return 0.55 * overlap + 0.45 * ih - depth_pen, hit


def score_url(t: Targeting, url: str, anchor_text: str = "", depth: int = 0) -> float:
    """Relevance of one candidate URL to the crawl goal. Higher = crawl sooner.
    Deterministic, cheap, range ~[-0.25, 1.0]. Used directly as the frontier
    sorted-set score (best-first).

    With NO goal tokens this reduces to `include_boost - depth_penalty`, i.e.
    shallow-first — reproducing the plain roughly-BFS ordering. Thin wrapper over
    `score_and_hit`; call that directly when the include verdict is also needed."""
    return score_and_hit(t, url, anchor_text, depth)[0]


async def rank_urls(t: Targeting, pairs, *, limit=None, depth: int = 0) -> list:
    """Score `(url, anchor_text)` pairs and return the best `limit` of them as
    `(url, anchor_text, score)`, best-first (ALL of them, fully sorted, when
    `limit` is None). Ties keep discovery order, like the stable sorts this
    replaces.

    The one shared ranking loop for every candidate list (map endpoints, the
    seeder, shard-completion admission). Two properties matter:
      - bounded heap: top-N is O(n log limit), not a full sort of a site-sized
        candidate dump just to serialize its head;
      - it YIELDS the event loop every ~2k URLs — candidate lists are
        site-controlled (an 8 MB sitemap is ~100k URLs) and these loops run
        inside async handlers, where one sync sweep stalls the coordinator."""
    bound = int(limit) if limit is not None and limit > 0 else None
    heap: list = []
    seq = 0
    for url, anchor in pairs:
        seq += 1
        s, _hit = score_and_hit(t, url, anchor or "", depth)
        entry = (s, -seq, url, anchor or "")   # -seq: earlier discovery wins ties
        if bound is None or len(heap) < bound:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            heapq.heapreplace(heap, entry)
        if (seq & 0x7FF) == 0:
            await asyncio.sleep(0)
    heap.sort(reverse=True)
    return [(u, a, s) for (s, _neg, u, a) in heap]


def passes_threshold(t: Targeting, score: float, depth: int, hit: bool) -> bool:
    """Keep decision for the relevance threshold. Seed/sitemap/operator-picked URLs
    (depth 0) and include-path hits are never dropped; otherwise a URL must clear
    the threshold (0.0 = threshold disabled, keep everything)."""
    if depth <= 0 or hit or t.threshold <= 0.0:
        return True
    return score >= t.threshold


# --------------------------------------------------------------------------- #
# Scope derivation (intent → include/exclude/max_depth) via the configured AI   #
# --------------------------------------------------------------------------- #
_SCOPE_SYSTEM = (
    "You translate a website crawl GOAL plus a SAMPLE of the site's real URLs "
    "into a crawl scope. Return ONLY a JSON object — no prose, no markdown, no "
    "code fence — with exactly these keys:\n"
    '  "include_paths": array of Python-regex strings matched with re.search '
    'against the URL PATH only (e.g. "^/blog/", "/docs/", "/api/"). Prefer a FEW '
    "broad patterns over many narrow ones. Base them on the sample's real paths.\n"
    '  "exclude_paths": array of Python-regex strings for paths to skip '
    '(e.g. "/tag/", "/author/", "/login").\n'
    '  "max_depth": integer 0-10 — how many link hops from the seed to follow.\n'
    '  "reason": one short sentence explaining the derived scope.\n'
    "If the goal is vague or clearly covers the whole site, return empty arrays "
    "and max_depth 4. Never invent path shapes that do not resemble the sample."
)

_SCOPE_DEFAULT = {"include_paths": [], "exclude_paths": [], "max_depth": 4, "reason": ""}


def _valid_regexes(v) -> list:
    """Keep only patterns that are both syntactically valid AND safe to execute.

    `re.compile` alone proves nothing about runtime cost — `(a+)+$` compiles
    happily and then backtracks catastrophically. These patterns are matched
    against every discovered URL of a crawl, so a bad one is amplified across the
    whole run. Drop anything validate_regex rejects instead of storing it.
    """
    from security.validation import InputValidator

    out: list = []
    if isinstance(v, list):
        for p in v:
            if isinstance(p, str) and p.strip():
                try:
                    re.compile(p)
                except re.error:
                    continue
                try:
                    InputValidator.validate_regex(p.strip())
                except Exception:
                    logger.warning("dropping ReDoS-prone crawl path pattern: %r", p)
                    continue
                out.append(p.strip())
    return out


def _parse_scope(text: str) -> dict:
    """Lenient parse of a model reply into a scope dict. Tolerates a bare object
    or one wrapped in prose / a ```json fence. Never raises; returns whole-site
    defaults on anything unparseable."""
    if not text:
        return dict(_SCOPE_DEFAULT)
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    obj = None
    try:
        obj = json.loads(s)
    except Exception:
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                obj = json.loads(s[i:j + 1])
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        return dict(_SCOPE_DEFAULT)
    try:
        md = max(0, min(10, int(obj.get("max_depth", 4))))
    except Exception:
        md = 4
    reason = obj.get("reason")
    return {
        "include_paths": _valid_regexes(obj.get("include_paths")),
        "exclude_paths": _valid_regexes(obj.get("exclude_paths")),
        "max_depth": md,
        "reason": (reason[:300] if isinstance(reason, str) else ""),
    }


async def derive_scope(intent: str, sample_urls, seed_url: str) -> dict:
    """One AI call: goal + a sample of the site's real URLs → a scope dict
    {include_paths, exclude_paths, max_depth, reason}. Best-effort; NEVER raises.

    Returns whole-site defaults when the goal is empty, no AI provider is
    configured (the common self-host case — a crawl must still work with no AI at
    all), or the call fails. The DETERMINISTIC half of this module (score_url /
    passes_threshold) needs no AI and always runs, so an operator with no model
    configured still gets relevance-ranked ordering from the goal's own tokens."""
    intent = (intent or "").strip()
    if not intent:
        return dict(_SCOPE_DEFAULT)
    # Dedupe + bound the sample so the call stays cheap. Full URLs give the model
    # host context; only the paths matter for the scope it returns.
    sample_lines = list(dict.fromkeys(u for u in (sample_urls or []) if u))[:150]
    sample = "\n".join(sample_lines) or "(no sample available)"
    from services import agent_brain
    try:
        text, _in, _out, _model = await agent_brain.call_ai(
            messages=[{"role": "user",
                       "content": f"SEED: {seed_url}\nGOAL: {intent}\n\nSAMPLE URLS:\n{sample}"}],
            system_prompt=_SCOPE_SYSTEM,
            max_tokens=600,
            purpose="crawl_scope",
        )
    except Exception:
        return dict(_SCOPE_DEFAULT)
    return _parse_scope(text)


def merge_scope(derived: dict, *, include_paths=None, exclude_paths=None,
                max_depth=None) -> dict:
    """Override semantics: an explicitly-provided value wins over the derived one.
    `None` means 'the caller did not provide it' (so the derived value is used).
    Returns the effective {include_paths, exclude_paths, max_depth}."""
    d = derived or {}
    eff_depth = max_depth if max_depth is not None else d.get("max_depth")
    try:
        eff_depth = int(eff_depth) if eff_depth is not None else 4
    except Exception:
        eff_depth = 4
    return {
        "include_paths": include_paths if include_paths is not None else (d.get("include_paths") or []),
        "exclude_paths": exclude_paths if exclude_paths is not None else (d.get("exclude_paths") or []),
        "max_depth": eff_depth,
    }
