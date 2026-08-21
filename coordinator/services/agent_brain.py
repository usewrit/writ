"""
Shared agent BRAIN for Writ's unified, mode-aware AI agent.

This module is the SINGLE source of truth for the agent loop's decision-making:
the system prompts, the per-turn user-message assembly, the gateway call, lenient
JSON parsing with a self-correct retry envelope, and the decision-coercion logic.

It is consumed by BOTH:
  - the interactive HTTP endpoint  the coordinator ai-assist router  POST /ai-assist/agent
  - the backend-orchestrated autonomous AI session loop

Keeping all of this here guarantees there is NO prompt drift between the
interactive and autonomous paths.

IMPORTANT layering note: this module has NO db / auth side effects.
Callers own:
  - persistence (generated steps / ai_conversation),
  - request/response HTTP DTOs.
The brain returns plain values (an AgentTurn dataclass) and never touches the DB.

The only side-effecting dependency is the AI gateway call (call_ai), which raises
fastapi.HTTPException on transport error. The orchestrator should catch Exception
broadly rather than rely on HTTP status semantics.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AI gateway call (the brain's only side-effecting dependency)
# ---------------------------------------------------------------------------

async def call_ai(
    messages: list,
    system_prompt: str = "",
    max_tokens: int = 1500,
    purpose: str = "assist",
    user: str = None,
    # Explicit model override (e.g. an AI session's `ai_model`). None = let the
    # configured provider's own model (or its default) decide.
    model: str = None,
    # BYO routing: when set, the completion runs through this callable (e.g. on the
    # user's OWN local agent keys) instead of the managed AI gateway — same
    # signature + return shape as ai_gateway_client.complete.
    complete_override=None,
    # Legacy params kept for backward compat — ignored when gateway is available
    ai_keys: dict = None,
    messages_anthropic: list = None,
    messages_openai: list = None,
) -> tuple:
    """
    Call AI via the gateway. Returns (text, input_tokens, output_tokens, model).

    `user` is a stable conversation id forwarded to the provider so streaming-backed
    providers route each conversation to its own tab.
    """
    from services.ai_gateway_client import complete as _gateway_complete, AIGatewayError
    complete = complete_override or _gateway_complete

    try:
        result = await complete(
            messages=messages,
            system=system_prompt if system_prompt else None,
            max_tokens=max_tokens,
            purpose=purpose,
            model=model,
            user=user,
        )
        return (
            result.get("content", "").strip(),
            result.get("usage", {}).get("input_tokens", 0),
            result.get("usage", {}).get("output_tokens", 0),
            result.get("model"),
        )
    except AIGatewayError as e:
        if is_context_overflow(e):
            logger.warning("[Agent Brain] provider rejected the prompt as too long: %s", str(e)[:400])
            raise HTTPException(
                status_code=400,
                detail=("This conversation has grown past what the configured AI model can read in "
                        "one request. Start a new chat to reset the thread, or set a model with a "
                        "larger context window in Settings → AI."),
            )
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "AI request failed. Please try again.", status_code=502, action="agent_brain.gateway")
    except Exception as e:
        from services.error_reporting import internal_http_error
        raise internal_http_error(e, "AI request failed. Please try again.", action="agent_brain.call")


# ---------------------------------------------------------------------------
# Context-overflow detection
# ---------------------------------------------------------------------------
# Provider phrasings for "this prompt does not fit in the context window". Both the
# self-host in-process path and the managed gateway put the provider's response body
# into the AIGatewayError message, so matching here covers every provider.
#
# This is the ONE AI transport failure the user can actually act on (start a new
# chat / pick a bigger model), so it is deliberately NOT flattened into the generic
# "AI request failed (ref: …)" — see services.error_reporting for the policy and its
# user-actionable carve-outs.
_CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long",
    "context length",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "reduce the length of the messages",
    "exceed context limit",
)


def is_context_overflow(err: BaseException) -> bool:
    """Whether a provider error means the prompt exceeded the model's context window."""
    text = str(err).lower()
    return any(m in text for m in _CONTEXT_OVERFLOW_MARKERS)


# ---------------------------------------------------------------------------
# JSON parse + self-correct helpers (pure)
# ---------------------------------------------------------------------------

def strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


_INVALID_JSON_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def escape_invalid_json_backslashes(s: str) -> str:
    r"""Escape lone backslashes that aren't a valid JSON escape.

    Models routinely emit code in their JSON — e.g. a regex like /^\d+$/ or a
    Windows path — where `\d`, `\s`, `\.` etc. are INVALID JSON escape sequences,
    so json.loads rejects the whole reply and the action is silently dropped to a
    chat message. Doubling those backslashes makes it parseable. Applied ONLY as a
    fallback after a normal parse fails, so well-formed JSON is never altered.
    """
    return _INVALID_JSON_ESCAPE.sub(r'\\\\', s)


_CTRL_ESCAPE = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}


def repair_unescaped_quotes(s: str) -> str:
    r"""Escape double quotes (and raw control chars) that appear INSIDE a JSON
    string value but were left unescaped by the model.

    Models embed JS in their reply's string fields and routinely forget to escape
    inner double quotes — overwhelmingly a CSS attribute selector such as
    document.querySelector('link[rel="canonical"]') sitting inside a single-quoted
    JS string. The raw " ends the JSON string early and json.loads aborts at the
    first stray quote, so the whole turn is dropped to "invalid".

    We walk the text tracking string state. A " encountered inside a string is
    treated as the string TERMINATOR only when the next non-space char is a
    structural delimiter (':' , ',' , '}') or end-of-input; otherwise it is a
    literal and we escape it. ']' is deliberately NOT a terminator: in this reply
    schema string values are never array elements (actions/steps_to_add hold
    objects), so '"]' is almost always the tail of an attribute selector
    (="x"]) rather than end-of-string + close-array. Raw newlines/tabs inside a
    string (also invalid JSON) are escaped too.

    Best-effort and applied ONLY as a post-failure fallback whose output is
    re-parsed: a wrong guess simply fails to parse and falls through to the retry
    path, so this can never corrupt a reply that already parses."""
    out = []
    in_string = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        # inside a string
        if ch == '\\':  # copy the escape pair verbatim (incl. a valid \" )
            out.append(ch)
            if i + 1 < n:
                out.append(s[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch in _CTRL_ESCAPE:
            out.append(_CTRL_ESCAPE[ch])
            i += 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and s[j] in ' \t\r\n':
                j += 1
            nxt = s[j] if j < n else ''
            if nxt in (':', ',', '}') or nxt == '':
                out.append(ch)
                in_string = False
            else:
                out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def loads_lenient(text: str) -> dict:
    r"""Parse a JSON object from a model reply, tolerating code fences, stray prose,
    and the JSON-escaping mistakes models make when embedding JS in a string field:
    invalid backslash escapes (regex \d, Windows paths, …) and unescaped double
    quotes / raw control chars inside the script (a CSS selector like [rel="x"]).

    Each repair is applied ONLY as a fallback after a strict parse fails, and the
    repaired text is re-parsed — so a well-formed reply is never altered and a wrong
    guess can only fall through to the retry path, never corrupt a good reply."""
    t = strip_code_fence(text)
    bases = [t]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        bases.append(t[start:end + 1])
    # Increasing aggressiveness. Backslash repair runs BEFORE quote repair so the
    # `\"` the quote pass inserts is not re-doubled by the backslash pass.
    repairs = (
        lambda s: s,
        escape_invalid_json_backslashes,
        repair_unescaped_quotes,
        lambda s: repair_unescaped_quotes(escape_invalid_json_backslashes(s)),
    )
    for c in bases:
        for repair in repairs:
            try:
                return json.loads(repair(c))
            except json.JSONDecodeError:
                continue
    raise json.JSONDecodeError("no parseable JSON object in model reply", t, 0)


# ---------------------------------------------------------------------------
# Context summarizers (pure, stdlib json only)
# ---------------------------------------------------------------------------

def summarize_network_calls(calls: list, max_calls: int = 30, body_chars: int = 400) -> str:
    """Compact, token-bounded summary of captured API calls for AI prompts.

    Each line: `[i] METHOD url -> status (content-type)` plus truncated request/
    response bodies and auth-relevant headers, so the model can match a UI
    sequence to the request it triggered.
    """
    if not calls:
        return "  (no API calls were captured during recording)"
    auth_hdr = ("authorization", "x-auth", "x-api-key", "x-token", "x-csrf", "cookie")
    lines = []
    for i, c in enumerate(calls[:max_calls]):
        if not isinstance(c, dict):
            continue
        method = c.get("method", "?")
        url = c.get("url", "?")
        status = c.get("response_status", "?")
        ctype = (c.get("response_content_type") or "").split(";")[0]
        lines.append(f"  [{i}] {method} {url} -> {status} ({ctype})")
        rb = c.get("request_body")
        if rb:
            lines.append(f"      req_body: {str(rb)[:body_chars]}")
            # The agent reveals held credentials in the body as {{placeholders}}. A POST/PUT sign-in
            # body carrying one is reconstructable → point the model at replaying it as a login_post.
            if str(method).upper() in ("POST", "PUT") and "{{" in str(rb):
                lines.append(
                    "      ↑ this sign-in body is reconstructable from your held credentials — you can replace the DOM login with a login_post step (this url + method + Content-Type + this {{placeholder}} body). Skip it if the body also carries a token you do NOT hold (csrf/nonce/authenticity)."
                )
        resp = c.get("response_body")
        if resp:
            lines.append(f"      resp_body: {str(resp)[:body_chars]}")
        for h, v in (c.get("request_headers") or {}).items():
            if any(p in h.lower() for p in auth_hdr):
                lines.append(f"      header {h}: {str(v)[:40]}")
    if len(calls) > max_calls:
        lines.append(f"  ...and {len(calls) - max_calls} more calls (truncated)")
    return "\n".join(lines)


def _step_origin(url: str) -> Optional[str]:
    """`scheme://host[:port]` origin of a step URL, or None for a relative/unparseable one."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url or "")
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return None


def prune_navigates_before_api_only(steps: list) -> list:
    """Drop a NAVIGATE whose segment (up to the next navigate) is ONLY api_call/login_post steps —
    those fetch their URL directly, so no positioning navigate is needed. Origin-safe and NEVER
    removes the ENTRY navigate (first one). Mirrors the Rust
    `explorer::prune_navigates_before_api_only`. Pure; returns a new list.
    """
    def ty(s):
        return (s.get("type") or "") if isinstance(s, dict) else ""

    def step_url(s):
        cfg = (s.get("config") or {}) if isinstance(s, dict) else {}
        return cfg.get("url") or (s.get("url") if isinstance(s, dict) else "") or ""

    out = list(steps)
    entry_idx = next((i for i, s in enumerate(out) if ty(s) == "navigate"), None)
    if entry_idx is None:
        return out
    entry_origin = _step_origin(step_url(out[entry_idx]))
    if not entry_origin:
        return out

    def is_request(s):
        return ty(s) in ("api_call", "login_post")

    def same_origin(s):
        o = _step_origin(step_url(s))
        return o == entry_origin if o else False

    i = 0
    while i < len(out):
        if ty(out[i]) != "navigate" or i == entry_idx:
            i += 1
            continue
        j = i + 1
        while j < len(out) and ty(out[j]) != "navigate":
            j += 1
        segment = out[i + 1:j]
        if segment and all(is_request(s) for s in segment) and all(same_origin(s) for s in segment):
            del out[i]
        else:
            i += 1
    return out


def summarize_steps(steps: list, max_steps: int = 60) -> str:
    """Compact one-line-per-step summary for AI context (not the full JSON)."""
    if not steps:
        return "  (nothing recorded yet)"
    lines = []
    for i, s in enumerate(steps[:max_steps]):
        if not isinstance(s, dict):
            continue
        t = s.get("type", "?")
        cfg = s.get("config") or {}
        target = s.get("selector") or s.get("url") or cfg.get("url") or cfg.get("selector") or ""
        val = s.get("value") or cfg.get("value") or ""
        if t == "fill" and (s.get("options", {}) or {}).get("is_sensitive"):
            val = "••••••"
        extra = f" = {str(val)[:40]}" if val else ""
        # Surface the stable step id so the model can target it in `step_edits`
        # (update/delete/move) rather than only appending new steps.
        sid = s.get("id")
        idtag = f" [id={sid}]" if sid else ""
        lines.append(f"  {i}{idtag}. {t} {str(target)[:80]}{extra}".rstrip())
    if len(steps) > max_steps:
        lines.append(f"  ...and {len(steps) - max_steps} more steps")
    return "\n".join(lines)


def summarize_scraper_history(history: list, max_turns: int = 10, char_budget: int = 40000) -> str:
    """View of prior turns (thoughts + actions + results). Kept DETAILED — the model
    needs to see its own full scripts and the real data they returned to reason and
    verify. Per-item caps are high (only guard against pathological blobs); the
    overall char_budget keeps the most recent turns when the history gets very big."""
    if not history:
        return "  (no actions run yet — this is the first turn)"
    # Rendered per TURN so the budget can be applied on a turn boundary below. A flat
    # character tail-cut used to slice mid-JSON, so the oldest thing the model read was
    # a fragment of a serialized blob.
    per_turn: List[str] = []
    for turn in history[-max_turns:]:
        if not isinstance(turn, dict):
            continue
        lines = []
        thought = str(turn.get("thought", ""))[:1000]
        lines.append(f"- thought: {thought}")
        for a in (turn.get("actions") or [])[:12]:
            lines.append(f"    ran: {json.dumps(a)[:6000]}")
        for r in (turn.get("results") or [])[:12]:
            lines.append(f"    result: {json.dumps(r)[:12000]}")
        per_turn.append("\n".join(lines))

    # Drop WHOLE turns from the oldest end — never a partial one — so the earliest
    # thing the model reads is a complete decision, not a fragment.
    total = sum(len(t) + 1 for t in per_turn)
    dropped = 0
    while total > char_budget and len(per_turn) > 1:
        total -= len(per_turn.pop(0)) + 1
        dropped += 1
    if dropped:
        per_turn.insert(0, f"- ({dropped} earlier turn(s) omitted to fit the context budget)")
    return "\n".join(per_turn)


# ---------------------------------------------------------------------------
# Prompt constants (pure data)
# ---------------------------------------------------------------------------

# Few-shot: a real list+detail+pagination scraper (employee list inside an iframe,
# per-row drill-down, browser-style pagination). Teaches the shape of a "done" script.
SCRAPER_EXAMPLE = """(async () => {
  function getDoc() {
    for (const f of document.querySelectorAll('iframe')) {
      if (f.src && f.src.includes('employee')) return f.contentDocument || f.contentWindow.document;
    }
    return document;
  }
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function readList() {
    const d = getDoc(), rows = d.querySelectorAll('table tr'), out = [];
    for (let i = 2; i < rows.length; i++) {
      const c = rows[i].querySelectorAll('td');
      if (c.length < 5) continue;
      out.push({ id: c[1].innerText.trim(), name: c[2].innerText.trim(), _btn: rows[i].querySelector('input.button') });
    }
    return out;
  }
  function readDetail() {
    const d = getDoc(), f = {};
    for (const name of ['employee_email','employee_tel_mobile','employee_address'])
      { const el = d.querySelector('[name="'+name+'"]'); if (el) f[name] = el.value || ''; }
    return f;
  }
  const all = []; let page = 1;
  while (true) {
    const list = readList();
    if (!list.length) break;
    for (const row of list) {
      const btn = row._btn; delete row._btn;
      if (btn) { btn.click(); await sleep(1500); Object.assign(row, readDetail()); getDoc().defaultView.history.back(); await sleep(1200); }
      all.push(row);
    }
    page++;
    const next = Array.from(getDoc().querySelectorAll('table tr:last-child a')).find(a => a.innerText.trim() === String(page));
    if (!next) break;
    next.click(); await sleep(1500);
  }
  return { total: all.length, employees: all };
})()"""


AGENT_BASE = """You are an AI agent embedded in a web-automation RECORDER. The user is building an automation and can ask you, at any time, to do something on the CURRENT page in a live browser that YOU control. You operate in a loop.

Each turn you receive: the user's request and the conversation, the current page as an OBSERVATION (url/fields/buttons/page text), the steps recorded so far, captured API calls, and the results of any actions you already ran this task. Screenshots are NOT sent automatically — when you need to SEE the page (layout, images, visual state), request one with the get_screenshot action and it will be attached to your next turn. Respond with ONE JSON object — exactly one of:

- {"action":"ask","thought":"...","message":"<your reply to the user>"}
    Use when the request is conversational, ambiguous (ask a clarifying question), or already satisfied. No browser action is taken.
- {"action":"run_actions","thought":"...","actions":[ ...action objects... ]}
    Drive the browser to explore / verify / test before committing.
- {"action":"done","thought":"...","summary":"<one line>", <MODE-SPECIFIC OUTPUT FIELDS>}
    PROPOSE your finished step/script for the USER to review and Apply. You do NOT finalize or save anything — this is a proposal; the user accepts it. Only propose AFTER you have actually verified it with run_actions/evaluate_js (run the script and confirm it returns real data). Never propose on faith or claim you "verified" something you did not run.

Browser ACTIONS you may put in "actions" (these are EPHEMERAL — they execute on the live page but are NOT recorded as workflow steps; they exist only to help you understand/verify):
- {"action":"navigate","url":"https://..."}
- {"action":"click","selector":"css"}   (or {"field_index":N} / {"button_index":N} from the observation)
- {"action":"fill","selector":"css","value":"text"}
- {"action":"select","selector":"css","value":"option"}
- {"action":"press_key","key":"Enter"}
- {"action":"scroll","direction":"down","amount":800}
- {"action":"back"}
- {"action":"wait","seconds":1.5}
- {"action":"read_text","selector":"css"}
- {"action":"capture_network"}   (reload the page with passive network capture; returns the page's backend API calls — use this to discover APIs instead of fetch())
- {"action":"evaluate_js","script":"<JS expression or async IIFE that returns JSON>"}   (your main probing/testing tool)
- {"action":"get_screenshot"}   (capture the full viewport to SEE the page; attached to your next turn)
- {"action":"get_screenshot","x":0,"y":0,"width":800,"height":600}   (capture just that region/block — use when you only need to look at one part)

EDITING EXISTING WORK: the steps already recorded are shown to you WITH a stable id (STEPS RECORDED SO FAR lists each line as `<i> [id=<id>] <type> ...`). A "done" is not limited to adding — it may also MODIFY what is already there. To change, remove, or reorder existing steps include a "step_edits" array alongside (or instead of) "steps_to_add". Each entry is exactly one of:
- {"op":"update","id":"<step id>","step":{ <only the fields to change — a nested "config" object is merged key-by-key> }}
- {"op":"delete","id":"<step id>"}
- {"op":"move","id":"<step id>","to":<new zero-based index>}
Reference each step by its id (use "index":<i> only when no id is shown). Change ONLY the steps the user asked you to change — never rewrite the whole list when a targeted edit will do, and never re-add a step that already exists.

RULES:
- You never finalize — every "done" is a PROPOSAL the user reviews and applies. So make it good: use evaluate_js to inspect and TEST on REAL data before you propose.
- Keep batches fast (<60s); never run a huge multi-page operation while exploring.
- Do things yourself rather than asking the user to.
- Return ONLY the output tied to THIS request (usually one step). Do not duplicate steps already recorded.
- Reply with ONLY the JSON object — no markdown, no prose outside it.
- Your "script" travels INSIDE a JSON string, so every double-quote in the code must be written as \\" or the whole reply fails to parse. The usual culprit is a quoted CSS attribute selector. Avoid it: query the tag and filter in JS instead, e.g. NOT querySelector('link[rel="canonical"]') but Array.from(document.querySelectorAll('link')).find(l => l.rel === 'canonical'); NOT [type="application/rss+xml"] but filter on l.type === 'application/rss+xml'. Keep all string/regex literals in the script single-quoted and free of backslash escapes.
"""

_AGENT_MODE_MANUAL = """MODE: MANUAL RECORDING. The workflow is a list of replayable steps. When done, put the step(s) that fulfill the request in "steps_to_add" (an array, in execution order). Step shapes:

EXTRACTION (when the goal is to READ/scrape data, or the user asks for a SCRIPT):
- DEFAULT — a script step: {"type":"evaluate","description":"...","config":{"variable":"<name>","script":"(async () => { ...; return { total, items }; })()","iframe":<iframe-src substring or null>}}
    Use "evaluate" for ANYTHING scripted or structured: lists, tables, multiple fields, computed values, per-item detail drill-down, pagination. The script is the BODY of the step — it does the querying itself and RETURNS the data as JSON. When the user says "write/create a script", this evaluate step IS the deliverable.
- ONE element's visible text only: {"type":"extract","description":"...","config":{"selector":"<css>","variable":"<name>"}}
    Reads a single element's text via a CSS selector you verified in the observation. NEVER emit "extract" carrying a script — a scripted extraction is an "evaluate" step; an extract without a working selector fails at replay ("Primary selector not found for extract"). When in doubt, use "evaluate".

INTERACTION (when the goal is to DO something — log in, fill/submit a form, click through a flow). Emit an ORDERED list of these replayable steps (one per user action), targeting a robust CSS selector you confirmed exists in the observation/a11y tree:
- navigate: {"type":"navigate","description":"...","config":{"url":"https://..."}}
- click (also how you SUBMIT — click the submit/login button): {"type":"click","description":"...","config":{"selector":"<css>","options":{"text":"<visible label, optional fallback>"}}}
- fill an input: {"type":"fill","description":"...","config":{"selector":"<css>","value":"<text>"}}
- type (when fill doesn't trigger JS handlers; types key-by-key): {"type":"type","description":"...","config":{"selector":"<css>","value":"<text>"}}
- select a dropdown option: {"type":"select","description":"...","config":{"selector":"<css>","value":"<option value or label>"}}
- check/uncheck a box: {"type":"check","description":"...","config":{"selector":"<css>"}} (or "uncheck")
- press a key (e.g. Enter to submit): {"type":"press","description":"...","config":{"key":"Enter"}}
- wait for the page to settle: {"type":"wait","description":"...","config":{"seconds":2}}
For credentials/secrets use placeholders in value: {{secret:password}}; for other dynamic user inputs use {{field_name}}. Do NOT hardcode real passwords.

API CALL (when the data should come from the site's BACKEND API — the user asks to use/call an endpoint, or a captured API call already returns the goal's data):
- {"type":"api_call","description":"...","config":{"method":"GET|POST|PUT","url":"<absolute URL>","headers":{...},"body_template":"<string or null>","response_extractions":{"<name>":"$.json.path"},"variable":"<name>"}}
    Prefer reusing a captured call (shown in your context; discover more with the capture_network action) — it replays with the session's cookies/auth. Parameterize dynamic values as {{placeholders}} and secrets as {{secret:name}}. NEVER wrap a fetch() inside an "evaluate" or "extract" script when an api_call step can make the request: api_call is the step type FOR endpoints — it needs no browser page, survives layout changes, and its response_extractions pull the fields out of the JSON directly.
    CSRF/XSRF: when the captured request carries an anti-CSRF header (X-XSRF-TOKEN, X-CSRF-Token, X-Requested-With+token, ...) whose value came from a COOKIE, you MUST reproduce that header — set it to the placeholder {{cookie:<cookie-name>}} (e.g. "X-XSRF-TOKEN":"{{cookie:XSRF-TOKEN}}"), which is read FRESH from the live cookie jar at every replay. Never bake the captured literal token (it expires — the replay 403s) and never drop the header (same result). Cookies themselves ride automatically; only the header/body ECHO of a cookie needs this placeholder.

Drive the flow with run_actions to confirm each selector works on the LIVE page BEFORE proposing; for extraction verify the script returns real data via evaluate_js. Extraction goals are usually ONE step; interaction goals are the ordered sequence of actions you performed. CHOOSING THE DELIVERABLE: endpoint/API goal -> api_call step | scripted/structured scrape -> evaluate step | one element's text -> extract step.

EXAMPLE of a good complex-scrape script (list + per-row detail + pagination inside an iframe):
""" + SCRAPER_EXAMPLE + """
"""

_AGENT_MODE_API = """MODE: API RECORDING. The user wants to call the site's backend directly instead of clicking the UI. Inspect the captured API calls (provided) and/or trigger the relevant request, then when done return an api_call step in "steps_to_add":
{"type":"api_call","description":"...","config":{"method":"GET|POST|PUT","url":"<absolute or relative URL>","headers":{...},"body_template":"<string or null>","response_extractions":{"<name>":"$.json.path"},"variable":"<name>"}}
Prefer reusing a captured call that matches the goal. Parameterize dynamic/user values as {{placeholders}} and secrets as {{secret:name}}. You may verify with an evaluate_js fetch (credentials:'include')."""

_AGENT_MODE_STREAMING = """MODE: STREAMING SCRIPT. This is a LONG-LIVED session — the page stays open, driven by a persistent `ps` (PageSession) runtime — so your deliverable is not a single recorded step but a richer ADVANCED SCRIPT you AUTHOR to DO things on the live page on demand. You have MORE freedom here than in manual recording: define real callable functions backed by full Playwright page access.

`ps` runtime:
- ps.fn("name", async ({ data, requestId }) => { ...; ps.respond(requestId, { success: true, result }); })  — a NAMED, independently-callable function. PREFER this: when the goal has DISTINCT operations, declare ONE ps.fn PER operation (e.g. "search", "get_details", "add_to_cart"), each doing its own work on the page. `data` carries the caller's arguments.
- ps.on("message", async ({ action, data, requestId }) => { ... })  — a single generic entry point that switches on `action` (use when one handler is enough).
- ps.page  — the Playwright Page; DO things on the page inside your functions: await ps.page.goto(url); await ps.page.click(sel); await ps.page.fill(sel, val); const v = await ps.page.evaluate(() => document.querySelector(sel)?.innerText).
- ps.respond(requestId, payload) replies to the caller; ps.emit(event, payload) pushes to subscribers; setInterval(...) does scheduled work.

When done, return the script in these fields, with "steps_to_add":[]:
{"script":"<script>","handler_name":"<short label, optional>","script_mode":"append"|"replace"}
The CURRENT ADVANCED SCRIPT (if any exists) is shown to you below — read it before you write. Pick the mode:
- "append" (DEFAULT) — "script" holds ONLY the NEW function(s) to ADD; it is appended after the current script. Use when building up / adding another ps.fn.
- "replace" — "script" is the COMPLETE new advanced script; it REPLACES the current one entirely. Use when the user asks you to CHANGE, FIX, RENAME, or REMOVE something in an existing handler: rewrite the whole script (keeping the parts you are not changing) and return it in full.
When there is no current script yet, either mode adds it. Add one function at a time when building up.

Verify selectors/behavior with evaluate_js against the LIVE page BEFORE proposing. Wrap each function body in try/catch and ps.respond an error on failure. JSON-SAFE SCRIPTS: your script is transported as JSON, so write NO backslash escape sequences inside string/regex literals — use String.fromCharCode(10) for newlines, plain [0-9]/[a-zA-Z] character classes, and .includes()/.startsWith()/.trim()/indexOf instead of escape-heavy regexes."""

AGENT_MODE_PROMPTS = {
    "manual": _AGENT_MODE_MANUAL,
    "api": _AGENT_MODE_API,
    "streaming": _AGENT_MODE_STREAMING,
}

# Appended ONLY for autonomous, backend-orchestrated AI sessions — never for the
# interactive /agent assist endpoint where a human is driving and evaluate_js stays
# the main probing tool. Two reasons this steering exists:
#   1. SECRETS: only the structured fill/type/click/select/check/press actions get
#      {{secret:name}} and {{field}} placeholders substituted with the real
#      user-supplied values at execution time. Raw evaluate_js receives the LITERAL
#      placeholder string (e.g. "{{secret:apikey}}"), so the secret is never
#      injected and the field gets garbage.
#   2. RECORDING: structured actions are recorded as replayable workflow steps;
#      evaluate_js mutations are not. An autonomous session's whole job is to
#      produce a working, replayable workflow.
# The recorder/agent ALSO enforces this: in autonomous sessions evaluate_js is
# READ-ONLY (a write/navigate/network/storage guard rejects mutating scripts), so
# any attempt to interact through raw JS errors out — interaction must use the
# structured actions.
_AGENT_AUTONOMOUS_ADDENDUM = """

AUTONOMOUS SESSION — NO HUMAN IS WATCHING, and your run_actions ARE the workflow being recorded. These rules OVERRIDE the generic action guidance above:

1. To INTERACT with the page (enter text, click, choose an option, check a box, submit, press a key) you MUST use the STRUCTURED actions: fill / type / click / select / check / press_key. They are the ONLY actions that (a) substitute {{secret:name}} and {{field}} placeholders with the real user-provided values at run time, and (b) get recorded as replayable steps.

2. evaluate_js is READ-ONLY here — use it freely to INSPECT/READ the page (query the DOM, extract values, check state, verify results), but it will REJECT any script that mutates the page, clicks, navigates, hits the network, or writes storage. NEVER try to fill a field or click through evaluate_js: raw JS does NOT substitute placeholders (it would write the literal "{{secret:apikey}}"), it is not recorded, and the read-only guard blocks it anyway. All interaction goes through the structured actions. To inspect the site's backend API traffic, use the capture_network action — NOT fetch()/XMLHttpRequest (those are blocked).

3. When a field needs a secret or user input, put the placeholder straight in the structured action's value, e.g. {"action":"fill","selector":"#apikey","value":"{{secret:apikey}}"}. The runtime injects the real value; you never see it and must never guess or hardcode it.

4. Your run_actions ARE the recording: every structured interaction you perform (navigate/fill/type/click/select/check/press_key) is automatically captured as a resilient, replayable workflow step. You therefore do NOT re-list those interactions in steps_to_add — they are recorded for you and re-listing duplicates them.

5. BUT your actual DELIVERABLE is still a step. The interactions only get you to the point where the goal can be fulfilled; the goal itself is almost always to RETURN DATA, so you MUST finish by proposing the step that produces it in steps_to_add, verified on real data first. Reaching the right page and stopping is NOT done. Only return "steps_to_add":[] in the rare case the goal is a pure interaction flow (e.g. just "log in") with no data to return.

6. For the data-return deliverable, DEFAULT to an "evaluate" step — a JS async IIFE that returns the data as JSON:
   {"type":"evaluate","description":"...","config":{"variable":"<name>","script":"(async () => { ... return { items: [...] }; })()"}}
   Use "evaluate" for ANYTHING structured: lists, tables, multiple fields, per-row drill-down, pagination. It is selector-agnostic (the script does the querying) and is the reliable path.
   Do NOT emit a bare {"type":"extract","config":{"selector":...}} for structured/list data — that path expects a single element's text via a CSS selector, and an evaluate-style goal routed through it fails ("Primary selector not found for extract"). Only use "extract" for one single element's text, and even then prefer evaluate. Verify the evaluate script actually returns real data (run it via evaluate_js / run_actions) before proposing.

7. CRITICAL — JSON-SAFE SCRIPTS. Your script string is transported as JSON, which corrupts backslash escapes: a newline escape you write inside a quoted string collapses into a REAL line break in transit and throws "SyntaxError: Invalid or unexpected token" at replay. So write scripts with NO backslash escape sequences inside string or regex literals. Concretely: to split text into lines use text.split(String.fromCharCode(10)) (NOT a quoted newline escape); use plain character classes like [0-9] for digits and [a-zA-Z] for letters (NOT the backslash-d / backslash-w shorthands); rely on .trim(), .includes(), .startsWith() and indexOf instead of regexes that need escapes. Keep the whole script on logical statements separated by semicolons. A script that ran during your own evaluate_js verification but uses escapes can still break once saved — so prefer the escape-free forms above.

8. TWO-FACTOR / ONE-TIME CODES. If the page asks for a one-time verification code — a "rotating PIN" / authenticator (TOTP) code, OR a code emailed to the account's inbox, OR a code sent by SMS — emit {"action":"twofa","thought":"...","selector":"<css of the code input, or omit to auto-detect>","submit_selector":"<css of the Verify/Continue button, optional>"}. ALL THREE channels (authenticator, EMAIL, SMS) are fully supported and equivalent here: the system retrieves the live code SERVER-SIDE from the configured persona — for an email challenge it reads the code straight from the persona's connected mailbox / OTP relay, for SMS from the SMS relay, for an authenticator it mints the TOTP. So an "email verification code" / "we sent a code to your email" / "check your inbox" challenge is NOT a blocker and NOT unsupported — it is exactly what this action handles; emit twofa for it. You yourself NEVER see, read, guess, type, or fill the code, and you do not need inbox access of your own — the backend does the reading; you must not manually open or scrape an inbox. This applies WHEREVER the challenge appears: a step AFTER the password, a modal/dialog that pops open, or a separate verification page. Do NOT use fill/type for a verification code; use this action. Do NOT divert to a "Continue with password" / alternate path just to avoid an email code — emit twofa instead. CHOOSING THE METHOD: a site usually defaults to ONE 2FA method but offers the others behind a "Try another way" / "Use a different method" / "More options" / "Can't access your authenticator?" / "Sign in another way" / "I can't use my Microsoft Authenticator app right now" / "Other ways to sign in" / "Try another method" link. If the method the page is currently demanding is one the configured persona CANNOT provide, but the persona supports a DIFFERENT method, click that switch-method control and pick the method the persona DOES have (e.g. switch an authenticator-app prompt to "Email a code", or switch an SMS prompt to the authenticator) BEFORE emitting twofa. Actively look for and exhaust these "try another way" options — open the list of alternatives and select the persona's method — and only conclude 2FA can't be completed once NO path to the persona's method remains. Never give up or close the session just because the FIRST method the site shows isn't the persona's. After it runs you get a fresh observation — continue the flow (you may still need to click Verify/Continue if you did not pass submit_selector, and there may be more steps after the code). A replayable 2FA step is recorded for you automatically, so do NOT add it to steps_to_add. Only use this when a code is actually being requested; if no persona 2FA is configured the action returns an error you can adapt to.

9. API-FIRST DELIVERABLES + LOG IN WITHOUT CLICKING. When the data you must return comes from a JSON endpoint (you see it in the CAPTURED API CALLS or via capture_network), prefer an "api_call" step over scraping the DOM — it returns the full, live data and survives layout changes: {"type":"api_call","description":"...","config":{"method":"GET","url":"<exact URL>","headers":{...only the auth header(s) the trace shows, with {{placeholders}}...},"body_template":null,"variable":"<name>"}}. Copy the auth header(s) EXACTLY as the trace reveals them (e.g. "Authorization":"Bearer {{login_key}}"); a cookie-authed endpoint needs no header. AND, once you've signed in via the form, look at the CAPTURED API CALLS for the sign-in POST — the one whose BODY shows your held credentials as {{placeholders}} (flagged in the trace). If it is reconstructable (only {{placeholders}} + static fields, NO csrf/nonce/authenticity token you don't hold, and not an SSO/redirect login), emit a "login_post" step to authenticate WITHOUT the form: {"type":"login_post","description":"Sign in via request","config":{"method":"POST","url":"<exact URL>","headers":{"Content-Type":"<as the trace shows>"},"body_template":"<exact {{placeholder}} body>"}}. Put it FIRST (after navigate); it establishes the session cookie so later api_call steps reuse it — the workflow becomes navigate → login_post → api_call, no fill/click login. If the sign-in body carries an unheld token or is SSO, KEEP the DOM login (fill/click) — do not force a login_post."""


# ---------------------------------------------------------------------------
# Brain decision / message assembly (pure)
# ---------------------------------------------------------------------------

def build_system_prompt(mode: str, autonomous: bool = False) -> tuple:
    """Normalize the mode and assemble the system prompt.

    Returns (normalized_mode, system_prompt). Unknown modes fall back to 'manual',
    exactly mirroring the /agent endpoint behavior.

    When ``autonomous`` is True (backend-orchestrated AI sessions), the
    structured-actions-only steering is appended so the brain never tries to
    fill/click through evaluate_js — which would skip
    {{secret:}} substitution, skip step recording, AND hit the recorder's raw-JS
    capability gate. The interactive /agent assist endpoint leaves this False so
    its behavior is unchanged (evaluate_js remains its main probing tool).
    """
    mode = (mode or "manual").lower()
    if mode not in AGENT_MODE_PROMPTS:
        mode = "manual"
    prompt = AGENT_BASE + "\n\n" + AGENT_MODE_PROMPTS[mode]
    if autonomous:
        prompt += _AGENT_AUTONOMOUS_ADDENDUM
    return mode, prompt


def _observation_text(observation: Optional[dict]) -> str:
    """Pack the page observation into bounded JSON text. The observation is already
    bounded at the SOURCE (the recorder caps the a11y tree to ~120 elements,
    fields/buttons to 40, page_text to ~3k), so it's forwarded WHOLE — no extra trim."""
    obs = observation or {}
    if not obs:
        return ""
    try:
        a11y = obs.get("a11y") or {}
        return json.dumps({
            "current_url": obs.get("current_url"),
            "viewport": a11y.get("viewport"),
            "fields": obs.get("fields", []),
            "buttons": obs.get("buttons", []),
            # Accessibility tree WITH coordinates: each element has role,
            # accessible name, tag, bounding box (x,y,w,h) + center (cx,cy)
            # in viewport pixels, and a selector. Use it to locate elements
            # and to aim get_screenshot regions.
            "a11y_elements": a11y.get("elements") or [],
            "page_text": obs.get("page_text", ""),
            "errors": obs.get("errors", []),
        })
    except (TypeError, ValueError):
        return str(obs)


# ---------------------------------------------------------------------------
# Multi-turn transcript assembly
# ---------------------------------------------------------------------------
#
# The agent loop used to flatten EVERYTHING (task, chat, prior decisions, action
# results, observation) into ONE `user` message that was rebuilt from scratch on
# every iteration. That has three costs: the model never sees its own prior replies
# as `assistant` turns (so it cannot tell "what I decided" from "what I was told",
# and the JSON it must self-correct against is not actually in the transcript), the
# provider can never cache a prefix (every iteration is a cold full-price read), and
# the only way to bound growth was a raw character slice that cut mid-JSON.
#
# We now build a REAL alternating thread:
#
#   user       TASK + the chat that led here          ← stable for the whole task
#   assistant  {"thought":…,"action":"run_actions",…} ← the model's OWN prior reply
#   user       RESULTS: …                             ← what actually happened
#   …                                                  (append-only ⇒ cacheable)
#   user       CURRENT STATE: steps / observation / API calls / iteration
#
# Everything that changes per turn lives in the FINAL user turn, so the prefix is
# append-only and a cache breakpoint on it is stable.

# Character budget for the REPLAYED transcript (the assistant/user pairs between the
# opening task turn and the final current-state turn) — roughly 30k tokens.
AGENT_THREAD_CHAR_BUDGET = 120_000
# The newest N turns keep their full action payloads and results. Older turns are
# condensed to their decision + per-result outcome BEFORE anything is dropped, and
# compaction always happens on a whole-turn boundary.
AGENT_THREAD_VERBATIM_TURNS = 4


def _turn_assistant_text(turn: dict) -> str:
    """The `assistant` message for one prior turn.

    Uses the model's OWN reply verbatim when the caller stored it (``assistant``),
    otherwise reconstructs the decision from the fields the loop recorded. Replaying
    the decision as a real assistant turn is what makes this a conversation rather
    than a prose summary of one, and it reinforces the JSON-only output discipline.
    """
    raw = turn.get("assistant")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:20000]
    decision: dict = {"thought": str(turn.get("thought") or "")[:2000]}
    actions = [a for a in (turn.get("actions") or []) if isinstance(a, dict)]
    if actions:
        decision["action"] = "run_actions"
        decision["actions"] = actions
    else:
        decision["action"] = str(turn.get("action") or "run_actions")
    return json.dumps(decision, default=str)


def _turn_results_text(turn: dict, *, body_chars: int = 12000, max_results: int = 12) -> str:
    """The `user` message carrying what the prior decision actually produced."""
    results = list(turn.get("results") or [])[:max_results]
    if not results:
        return "RESULTS: (the batch produced no results)"
    lines = ["RESULTS:"]
    for r in results:
        lines.append("  " + json.dumps(r, default=str)[:body_chars])
    return "\n".join(lines)


def _condense_turn(turn: dict) -> tuple:
    """(assistant_text, user_text) for an OLD turn — the decision keeps its thought and
    the action verbs/targets, each result collapses to its outcome.

    This replaces the old `out[-char_budget:]` character slice, which cut mid-JSON and
    left the model reading a fragment of a serialized blob as its earliest context."""
    actions = [a for a in (turn.get("actions") or []) if isinstance(a, dict)]
    brief = []
    for a in actions[:12]:
        verb = str(a.get("action") or "?")
        target = a.get("selector") or a.get("url") or a.get("key") or ""
        brief.append(f"{verb} {str(target)[:80]}".strip())
    assistant = json.dumps({
        "thought": str(turn.get("thought") or "")[:300],
        "action": "run_actions" if actions else str(turn.get("action") or "run_actions"),
        "actions_summary": brief,
    }, default=str)

    outcomes = []
    for r in list(turn.get("results") or [])[:12]:
        if not isinstance(r, dict):
            outcomes.append(str(r)[:120])
            continue
        verb = str(r.get("action") or "?")
        if r.get("error"):
            outcomes.append(f"{verb} ✗ {str(r['error'])[:160]}")
        elif r.get("verification"):
            outcomes.append(f"{verb} ✗ {str(r['verification'])[:200]}")
        elif "eval_result" in r:
            outcomes.append(f"{verb} ✓ returned {str(r.get('eval_result'))[:160]}")
        else:
            outcomes.append(f"{verb} ✓")
    user = "RESULTS (condensed):\n" + "\n".join("  " + o for o in outcomes) if outcomes \
        else "RESULTS (condensed): (none)"
    return assistant, user


def _render_history_messages(
    history: list,
    *,
    char_budget: int = AGENT_THREAD_CHAR_BUDGET,
    verbatim_turns: int = AGENT_THREAD_VERBATIM_TURNS,
) -> List[dict]:
    """Replay prior agent turns as alternating assistant/user messages, compacted to
    ``char_budget`` on WHOLE-TURN boundaries (condense oldest-first, drop only as a
    last resort and say so). Pure."""
    turns = [h for h in (history or []) if isinstance(h, dict)]
    if not turns:
        return []

    n = len(turns)
    rendered: List[list] = []
    for i, h in enumerate(turns):
        if i >= n - verbatim_turns:
            rendered.append([_turn_assistant_text(h), _turn_results_text(h)])
        else:
            rendered.append(list(_condense_turn(h)))

    def _total() -> int:
        return sum(len(a) + len(u) for a, u in rendered)

    # Pass 1 — condense from the oldest end (already-condensed turns are no-ops).
    for i in range(n):
        if _total() <= char_budget:
            break
        rendered[i] = list(_condense_turn(turns[i]))

    # Pass 2 — still over budget: drop the oldest turns outright, but TELL the model
    # they existed so it doesn't mistake the transcript for the whole task.
    dropped = 0
    while _total() > char_budget and len(rendered) > 1:
        rendered.pop(0)
        dropped += 1

    messages: List[dict] = []
    if dropped:
        messages.append({"role": "user", "content": [{
            "type": "text",
            "text": f"({dropped} earlier turn(s) of this task were dropped to fit the context "
                    f"budget — the summarized turns below are what remains.)",
        }]})
    for assistant_text, user_text in rendered:
        messages.append({"role": "assistant", "content": assistant_text})
        messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})
    return messages


def _collapse_same_role(messages: List[dict]) -> List[dict]:
    """Merge consecutive same-role turns so the thread strictly alternates — some
    providers reject two `user` messages in a row. String and block-list contents are
    both handled."""
    def _blocks(content):
        return content if isinstance(content, list) else [{"type": "text", "text": content or ""}]

    out: List[dict] = []
    for m in messages:
        if out and out[-1]["role"] == m["role"]:
            prev, cur = out[-1], m
            if isinstance(prev["content"], str) and isinstance(cur["content"], str):
                prev["content"] = prev["content"] + "\n\n" + cur["content"]
            else:
                prev["content"] = _blocks(prev["content"]) + _blocks(cur["content"])
        else:
            out.append(dict(m))
    return out


def _mark_cache_breakpoint(messages: List[dict]) -> None:
    """Tag the last STABLE user block with Anthropic's ``cache_control`` so the whole
    prefix — system prompt + opening task turn + every replayed turn — is served from
    cache instead of re-read at full price each iteration.

    Applied to the last user message BEFORE the final current-state turn: everything
    up to there is append-only, so the cached prefix stays valid across iterations.
    Anthropic reads it; ``local_ai._to_openai_messages`` rebuilds text blocks and
    drops it, so OpenAI/OpenRouter/Ollama are unaffected. Below the provider's
    minimum cacheable length it is simply ignored (not an error)."""
    for i in range(len(messages) - 2, -1, -1):
        m = messages[i]
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for block in reversed(m["content"]):
            if isinstance(block, dict) and block.get("type") == "text":
                block["cache_control"] = {"type": "ephemeral"}
                return
        return


def _format_conversation(conversation: list) -> str:
    """The chat that led to this task, as bounded dialogue lines. Each message is
    clamped so one pathological entry can't dominate the opening turn (the transport
    model has no per-message cap)."""
    def _role(m):
        return getattr(m, "role", None) if not isinstance(m, dict) else m.get("role")

    def _content(m):
        return getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")

    lines = [
        f"  {_role(m)}: {str(_content(m) or '')[:4000]}"
        for m in (conversation or [])
    ]
    return "\n".join(lines) or "  (start of conversation)"


def _current_state_text(
    *,
    page_url: str,
    observation: Optional[dict],
    steps: list,
    network_calls: list,
    iteration: int,
    max_iterations: int,
    advanced_script: str = "",
) -> str:
    """The final user turn: the authoritative state as of RIGHT NOW. Kept byte-for-byte
    compatible with the sections the one-shot prompt used, so nothing the model was
    trained-in-context to look for moved or changed name."""
    obs_text = _observation_text(observation)

    script_section = ""
    if advanced_script and advanced_script.strip():
        script_section = f"CURRENT ADVANCED SCRIPT:\n{advanced_script[:12000]}\n\n"

    return (
        f"CURRENT STATE\n"
        f"Current URL: {page_url}\n"
        f"Iteration: {iteration + 1} of {max_iterations}"
        f"{' — wrap up and finalize if you can.' if iteration >= max_iterations - 3 else ''}\n\n"
        f"STEPS RECORDED SO FAR:\n{summarize_steps(steps, max_steps=500)}\n\n"
        f"{script_section}"
        f"PAGE OBSERVATION:\n{obs_text or '  (none yet — run actions / evaluate_js to inspect the page)'}\n\n"
        f"CAPTURED API CALLS:\n{summarize_network_calls(network_calls, max_calls=60)}\n\n"
        "Decide the next step now and reply with the JSON object."
    )


def build_agent_messages(
    *,
    instruction: str,
    conversation: list,
    page_url: str,
    observation: Optional[dict],
    steps: list,
    history: list,
    network_calls: list,
    iteration: int,
    max_iterations: int,
    advanced_script: str = "",
    screenshot_b64: Optional[str] = None,
    attachments: Optional[list] = None,
    thread_char_budget: int = AGENT_THREAD_CHAR_BUDGET,
) -> List[dict]:
    """Assemble the agent turn as a REAL multi-turn thread (see the module note above).

    Layout: opening task turn (stable) → replayed assistant/user pairs (append-only,
    compacted on turn boundaries) → final current-state turn (rebuilt each iteration).
    Pure — no db/auth/network."""
    opening_text = (
        f"TASK: {instruction}\n\n"
        f"CONVERSATION SO FAR:\n{_format_conversation(conversation)}"
    )
    opening_content: list = []
    # User-attached files ride the OPENING turn: they are grounding for the whole
    # task and never change, so they sit inside the cacheable prefix.
    for block in (attachments or []):
        if isinstance(block, dict) and block.get("type") in ("image", "document"):
            opening_content.append(block)
    opening_content.append({"type": "text", "text": opening_text})

    messages: List[dict] = [{"role": "user", "content": opening_content}]
    messages.extend(_render_history_messages(history, char_budget=thread_char_budget))

    # Final turn — everything that changes per iteration.
    final_content: list = []
    if screenshot_b64:
        final_content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}})
    final_content.append({"type": "text", "text": _current_state_text(
        page_url=page_url, observation=observation, steps=steps,
        network_calls=network_calls, iteration=iteration,
        max_iterations=max_iterations, advanced_script=advanced_script,
    )})
    messages.append({"role": "user", "content": final_content})

    messages = _collapse_same_role(messages)
    _mark_cache_breakpoint(messages)
    return messages


def build_user_message(
    *,
    instruction: str,
    conversation: list,
    page_url: str,
    observation: Optional[dict],
    steps: list,
    history: list,
    network_calls: list,
    iteration: int,
    max_iterations: int,
    advanced_script: str = "",
    screenshot_b64: Optional[str] = None,
    attachments: Optional[list] = None,
) -> list:
    """Assemble the multimodal user turn (the `messages` list). Pure.

    `conversation` is a list of objects/dicts each exposing `.role`/`.content`
    (Pydantic models) or `["role"]`/`["content"]` (plain dicts).

    `attachments` (optional) is a list of pre-built Anthropic content blocks —
    ``{"type":"image","source":{...}}`` / ``{"type":"document","source":{...}}`` —
    for user-attached files (AI-session attachments, §9.1). They are prepended to
    the content list (before the screenshot/text) so the model sees the supplied
    file(s) as grounding. The caller resolves file_id → bytes/base64 and builds the
    blocks (the brain stays pure and never touches storage/auth).
    """
    obs_text = _observation_text(observation)

    def _role(m):
        return getattr(m, "role", None) if not isinstance(m, dict) else m.get("role")

    def _content(m):
        return getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")

    convo = "\n".join(
        f"  {_role(m)}: {_content(m)}" for m in (conversation or [])
    ) or "  (start of conversation)"

    # Streaming mode: show the current advanced script so the model can EDIT it
    # (script_mode:"replace") instead of only ever appending to it.
    script_section = ""
    if advanced_script and advanced_script.strip():
        script_section = f"CURRENT ADVANCED SCRIPT:\n{advanced_script[:12000]}\n\n"

    user_text = (
        f"USER REQUEST: {instruction}\n\n"
        f"CONVERSATION SO FAR:\n{convo}\n\n"
        f"Current URL: {page_url}\n"
        f"Iteration: {iteration + 1} of {max_iterations}"
        f"{' — wrap up and finalize if you can.' if iteration >= max_iterations - 3 else ''}\n\n"
        f"STEPS RECORDED SO FAR:\n{summarize_steps(steps, max_steps=500)}\n\n"
        f"{script_section}"
        f"PAGE OBSERVATION:\n{obs_text or '  (none yet — run actions / evaluate_js to inspect the page)'}\n\n"
        f"ACTIONS RUN THIS TASK:\n{summarize_scraper_history(history)}\n\n"
        f"CAPTURED API CALLS:\n{summarize_network_calls(network_calls, max_calls=60)}\n\n"
        "Decide the next step now and reply with the JSON object."
    )

    content: list = []
    # User-attached files first (AI-session attachments) so the model has the
    # supplied document/image as grounding before the task framing text.
    for block in (attachments or []):
        if isinstance(block, dict) and block.get("type") in ("image", "document"):
            content.append(block)
    if screenshot_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": screenshot_b64}})
    content.append({"type": "text", "text": user_text})
    return [{"role": "user", "content": content}]


@dataclass
class AgentTurn:
    """The brain's normalized decision for one turn. NO db/auth/credit side effects.

    action is one of: ask | run_actions | done | twofa | retry. The 'retry' value is
    NOT in the AgentChatResponse documented set but IS part of the brain's contract —
    it is returned when the model's reply could not be parsed as JSON, so the
    consuming loop re-loops with a corrective message. 'twofa' is emitted by
    autonomous sessions when the page asks for a one-time verification code; the
    runner mints + enters it server-side (the code never re-enters the LLM). Both
    consumers must handle it.
    """
    action: str = "ask"
    thought: str = ""
    message: str = ""
    actions: List[dict] = field(default_factory=list)
    steps_to_add: List[dict] = field(default_factory=list)
    # Edits to EXISTING steps (update/delete/move) and, for streaming, whether the
    # returned `script` appends to or replaces the current advanced script.
    step_edits: List[dict] = field(default_factory=list)
    script_mode: str = "append"
    # twofa action only: CSS of the OTP input + (optional) the verify/submit button.
    # Either may be empty — the agent auto-detects the field when omitted.
    selector: str = ""
    submit_selector: str = ""
    script: str = ""
    handler_name: str = ""
    variable: str = ""
    iframe: Optional[str] = None
    summary: str = ""
    # The model's reply VERBATIM. The loop stores it on the history turn so the next
    # iteration can replay it as a real `assistant` message instead of a lossy
    # reconstruction (see _turn_assistant_text). Bounded — it is a JSON decision, not
    # a transcript. Never surfaced to the user.
    raw_reply: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: Optional[str] = None


def sanitize_js_script(script: str) -> str:
    """Re-escape raw control characters that sit INSIDE a JS string literal.

    The model emits scripts as JSON; by the time we parse the reply, a newline the
    model wrote as an escape inside a quoted string has already collapsed into a
    REAL newline character — which is a hard JS syntax error ("Invalid or unexpected
    token") once the saved script is page.evaluate()'d at replay. This walks the
    source tracking string state and converts raw newline/CR/tab back to their
    escaped forms ONLY inside ' and " literals. Template literals (backticks) allow
    real newlines, and newlines between statements are left untouched, so legitimate
    formatting is preserved."""
    if not script or not any(ch in script for ch in ("\n", "\r", "\t")):
        return script
    out: list = []
    quote = None  # active string delimiter: ' " or `
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        if quote is None:
            if c in ("'", '"', '`'):
                quote = c
            out.append(c)
            i += 1
            continue
        # inside a string literal
        if c == "\\" and i + 1 < n:  # keep escape sequences intact
            out.append(c)
            out.append(script[i + 1])
            i += 2
            continue
        if c == quote:
            quote = None
            out.append(c)
            i += 1
            continue
        if quote != "`":  # ' or " — raw control chars are illegal here
            if c == "\n":
                out.append("\\n"); i += 1; continue
            if c == "\r":
                out.append("\\r"); i += 1; continue
            if c == "\t":
                out.append("\\t"); i += 1; continue
        out.append(c)
        i += 1
    return "".join(out)


def _sanitize_step_scripts(step: dict) -> dict:
    """Sanitize JS in a workflow step's config (script / computed-extract script)
    in place, so the saved step replays cleanly. Returns the same dict."""
    if not isinstance(step, dict):
        return step
    cfg = step.get("config")
    if isinstance(cfg, dict):
        if isinstance(cfg.get("script"), str):
            cfg["script"] = sanitize_js_script(cfg["script"])
        # computed extract stores its JS body under value/script
        if step.get("type") == "extract" and isinstance(cfg.get("value"), str) \
                and (cfg.get("options") or {}).get("extract_type") == "computed":
            cfg["value"] = sanitize_js_script(cfg["value"])
    return step


def _coerce_step_edits(raw) -> List[dict]:
    """Keep only well-formed edit ops (update/delete/move) that reference a target
    step by id or index. Sanitizes any JS embedded in an `update`'s step config so
    an edited evaluate/extract step still replays cleanly. Caps at 40 ops."""
    out: List[dict] = []
    for e in (raw or []):
        if not isinstance(e, dict):
            continue
        op = e.get("op")
        if op not in ("update", "delete", "move"):
            continue
        edit: dict = {"op": op}
        if e.get("id") is not None:
            edit["id"] = str(e.get("id"))[:200]
        if isinstance(e.get("index"), int):
            edit["index"] = e["index"]
        if op == "update":
            st = e.get("step")
            if not isinstance(st, dict):
                continue
            edit["step"] = _sanitize_step_scripts(st)
        if op == "move":
            if not isinstance(e.get("to"), int):
                continue
            edit["to"] = e["to"]
        # Must reference a target step.
        if "id" not in edit and "index" not in edit:
            continue
        out.append(edit)
    return out[:40]


def coerce_decision(parsed: dict) -> dict:
    """Action inference + clamping + field extraction from a parsed model reply.

    Mirrors ai_agent_turn's coercion (action inference, actions[:25],
    steps_to_add[:20]) and the AgentChatResponse field extraction/truncation.
    Returns a normalized dict of decision fields (no token/model fields).
    """
    action = parsed.get("action")
    if action not in ("ask", "run_actions", "done", "twofa"):
        action = "done" if (parsed.get("steps_to_add") or parsed.get("script") or parsed.get("step_edits")) else (
            "run_actions" if parsed.get("actions") else "ask")
    actions = [a for a in (parsed.get("actions") or []) if isinstance(a, dict)][:25]
    steps_to_add = [s for s in (parsed.get("steps_to_add") or []) if isinstance(s, dict)][:20]
    # JSON-transport sanitation: re-escape raw control chars inside JS string
    # literals so saved evaluate/extract steps (and ephemeral evaluate_js actions
    # used for verification) don't throw "Invalid or unexpected token" at replay.
    for _s in steps_to_add:
        _sanitize_step_scripts(_s)
    for _a in actions:
        if _a.get("action") == "evaluate_js":
            if isinstance(_a.get("script"), str):
                _a["script"] = sanitize_js_script(_a["script"])
            if isinstance(_a.get("value"), str):
                _a["value"] = sanitize_js_script(_a["value"])
    script_mode = parsed.get("script_mode")
    if script_mode not in ("append", "replace"):
        script_mode = "append"
    return {
        "action": action,
        "thought": str(parsed.get("thought", ""))[:2000],
        "message": str(parsed.get("message", ""))[:4000],
        "actions": actions,
        "steps_to_add": steps_to_add,
        "step_edits": _coerce_step_edits(parsed.get("step_edits")),
        "script": sanitize_js_script(str(parsed.get("script", ""))),
        "script_mode": script_mode,
        "handler_name": str(parsed.get("handler_name", ""))[:80],
        "variable": str(parsed.get("variable") or "")[:60],
        "iframe": (parsed.get("iframe") or None),
        "summary": str(parsed.get("summary", ""))[:600],
        # twofa action: the OTP field + verify button selectors (both optional).
        "selector": str(parsed.get("selector") or "")[:400],
        "submit_selector": str(parsed.get("submit_selector") or "")[:400],
    }


def _retry_turn(parse_err: Exception, raw_text: str) -> AgentTurn:
    """Build the unparseable-reply retry envelope (action='retry')."""
    cause = f"{type(parse_err).__name__}: {parse_err}"
    logger.warning(
        f"[Agent Brain] Unparseable reply ({cause}) — returning retry. "
        f"raw_len={len(str(raw_text))} raw[:1800]={str(raw_text)[:1800]!r}"
    )
    return AgentTurn(
        action="retry",
        thought="",
        message=(
            f"Your previous reply could NOT be parsed as JSON — error: {cause}. "
            "Reply again with ONLY one valid JSON object per the schema — no prose, no "
            "markdown/code fences, straight double quotes only. If a script contains a regex "
            "or path, avoid backslashes (use [0-9] instead of \\d, [a-z] instead of \\w)."
        ),
    )


async def run_agent_turn(
    *,
    mode: str,
    instruction: str,
    conversation: list,
    page_url: str,
    observation: Optional[dict],
    steps: list,
    history: list,
    network_calls: list,
    iteration: int,
    max_iterations: int,
    advanced_script: str = "",
    screenshot_b64: Optional[str] = None,
    conversation_id: Optional[str] = None,
    autonomous: bool = False,
    complete_override=None,
    attachments: Optional[list] = None,
    max_tokens: int = 3000,
    # Explicit model override (an AI session's `ai_model`). None = provider default.
    ai_model: Optional[str] = None,
    # Character budget for the replayed transcript. Lower it for a small-context
    # provider (a local model) so compaction kicks in before the provider rejects
    # the request; see AGENT_THREAD_CHAR_BUDGET.
    thread_char_budget: int = AGENT_THREAD_CHAR_BUDGET,
) -> AgentTurn:
    """Run ONE turn of the shared agent brain.

    1. build_system_prompt; 2. build_agent_messages (the real multi-turn thread);
    3. call_ai(max_tokens=3000, purpose="assist", user=conversation_id);
    4. loads_lenient with the retry-envelope fallback; 5. coerce_decision.

    Returns an AgentTurn. NO db / auth side effects — the caller does any
    persistence. call_ai may raise fastapi.HTTPException on gateway/transport error.
    """
    _mode, system_prompt = build_system_prompt(mode, autonomous=autonomous)

    messages = build_agent_messages(
        instruction=instruction,
        conversation=conversation,
        page_url=page_url,
        observation=observation,
        steps=steps,
        history=history,
        network_calls=network_calls,
        iteration=iteration,
        max_iterations=max_iterations,
        advanced_script=advanced_script,
        screenshot_b64=screenshot_b64,
        attachments=attachments,
        thread_char_budget=thread_char_budget,
    )

    ai_text, input_tokens, output_tokens, used_model = await call_ai(
        messages=messages,
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        purpose="assist",
        user=conversation_id,
        model=ai_model,
        complete_override=complete_override,
    )

    try:
        parsed = loads_lenient(ai_text)
    except (json.JSONDecodeError, AttributeError) as parse_err:
        # The model's reply wasn't a valid JSON object — hand the EXACT error back
        # to the agent LOOP (bounded by max_iterations) so it gets another turn to
        # fix it. Token counts are still surfaced so the caller bills the call.
        turn = _retry_turn(parse_err, ai_text)
        turn.input_tokens = input_tokens
        turn.output_tokens = output_tokens
        turn.model = used_model
        # Deliberately NOT carried: an unparseable reply must not be replayed as an
        # assistant turn, or the loop teaches the model that malformed output is
        # part of the conversation. The corrective message is the feedback.
        turn.raw_reply = ""
        return turn

    decision = coerce_decision(parsed)
    return AgentTurn(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=used_model,
        raw_reply=str(ai_text or "")[:20000],
        **decision,
    )
