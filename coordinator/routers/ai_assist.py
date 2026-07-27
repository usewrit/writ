"""
AI Assist router - AI-powered recording assistance endpoints.

Provides:
- Recording chat: tell AI what to do on the current page (returns instructions, not execution)
- Extract generation: describe what to extract, AI builds the extraction steps

Credit sources: ai_assist_chat, ai_assist_extract
"""
import logging
import json
import re
import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from utils.redis_client import get_redis
from models.config import Config
from security.dependencies import get_auth_context, AuthContext
from security.feature_gate import require_feature
from services import ai_limits
from services.error_reporting import internal_http_error
from services import agent_brain
# The agent BRAIN (prompts, gateway call, JSON parse+self-correct, summarizers,
# per-turn decision) lives in services.agent_brain so the interactive /agent
# endpoint and the autonomous orchestrator share ONE implementation (no prompt
# drift). Re-bind the names under their original private aliases so the many
# existing call sites in this router keep working unchanged.
from services.agent_brain import (
    call_ai as _call_ai,
    strip_code_fence as _strip_code_fence,
    loads_lenient as _loads_lenient,
    summarize_steps as _summarize_steps,
    summarize_network_calls as _summarize_network_calls,
    summarize_scraper_history as _summarize_scraper_history,
    SCRAPER_EXAMPLE as _SCRAPER_EXAMPLE,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai-assist", tags=["AI Assist"])


async def _rate_limit_ai(auth: AuthContext = Depends(get_auth_context)):
    """Rate limit AI endpoints: max 20 requests per minute for the owner."""
    if not auth.user_id:
        return
    try:
        from security.rate_limit import RateLimiter
        _redis = get_redis()  # shared pooled client (do not close)
        limiter = RateLimiter(_redis, max_requests=20, window_seconds=60, prefix="ai_assist_rl")
        # Fail CLOSED: these endpoints spend money on LLM calls, so if the
        # limiter can't run we must not let traffic through unthrottled.
        allowed, info = await limiter.is_allowed(str(auth.user_id), fail_open=False)
        if not allowed:
            raise HTTPException(status_code=429, detail="AI rate limit exceeded. Max 20 requests per minute.")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"AI rate limit check failed (denying request): {e}")
        raise HTTPException(status_code=503, detail="Rate limiter unavailable, please retry.")

def _decrypt_value(encrypted: str) -> str:
    if not encrypted:
        return ""
    from security.encryption import SecretEncryption
    try:
        return SecretEncryption.decrypt_secret(encrypted)
    except Exception:
        return ""


def _compute_cost_micros(input_tokens: int, output_tokens: int, model: str = None) -> int:
    # USD cost (micro-dollars) for this call at the model's admin-configured
    # per-token rate (model_pricing table; default rate for unknown models).
    from services.model_pricing_service import compute_cost_micros
    return compute_cost_micros(model, input_tokens, output_tokens)


async def _get_ai_keys(db: AsyncSession) -> dict:
    keys = {}
    for key_name in ["anthropic_api_key", "openai_api_key"]:
        result = await db.execute(select(Config).where(Config.key == key_name))
        config = result.scalar_one_or_none()
        if config and config.value:
            decrypted = _decrypt_value(config.value.get("encrypted", ""))
            if decrypted:
                keys[key_name] = decrypted

    for cfg_key in ["anthropic_model", "openai_model", "anthropic_base_url", "openai_base_url"]:
        result = await db.execute(select(Config).where(Config.key == cfg_key))
        cfg = result.scalar_one_or_none()
        if cfg and cfg.value and cfg.value.get("value"):
            keys[cfg_key] = cfg.value["value"]

    return keys


# --- Request / Response models ---

class AIChatMessage(BaseModel):
    role: str
    content: str


class AIChatRequest(BaseModel):
    screenshot_b64: str = Field(..., max_length=2_000_000, description="JPEG screenshot of current page as base64")
    page_url: str = Field(..., description="Current page URL")
    instruction: str = Field(..., max_length=5000, description="User instruction")
    conversation: List[AIChatMessage] = Field(default_factory=list, max_length=20, description="Prior messages for multi-turn context")
    context: Optional[str] = Field(None, description="Context mode: 'recording' (default), 'streaming_script'")
    page_dom: Optional[str] = Field(None, max_length=30000, description="DOM/accessibility tree of the current page")
    # Recording context so the multi-turn assistant knows what's already done.
    steps: List[dict] = Field(default_factory=list, max_length=500, description="Steps recorded so far")
    network_calls: List[dict] = Field(default_factory=list, max_length=60, description="API calls captured during recording")
    # Current streaming advanced script (streaming_script context) so the assistant
    # can EDIT the existing handlers rather than regenerate them blind.
    advanced_script: str = Field("", max_length=200_000, description="Current streaming advanced script, if any")


class AIChatResponse(BaseModel):
    message: str
    actions: List[dict] = Field(default_factory=list)
    credits_used: int = 0


class AIExtractRequest(BaseModel):
    screenshot_b64: str = Field(..., max_length=2_000_000, description="JPEG screenshot of current page as base64")
    page_url: str = Field(..., description="Current page URL")
    goal: str = Field(..., description="What the user wants to extract")
    page_html: Optional[str] = Field(None, max_length=50_000, description="Truncated page HTML for better selectors")
    steps: List[dict] = Field(default_factory=list, max_length=500, description="Steps recorded so far (page/navigation context)")


class AIExtractStep(BaseModel):
    selector: str
    output_name: str
    extract_type: str
    description: str
    config: dict = Field(default_factory=dict)


class AIExtractResponse(BaseModel):
    steps: List[AIExtractStep] = Field(default_factory=list)
    message: str = ""
    credits_used: int = 0


# --- Endpoints ---

@router.post("/chat", response_model=AIChatResponse, dependencies=[Depends(_rate_limit_ai), Depends(require_feature("ai_workflows"))])
async def ai_recording_chat(
    req: AIChatRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    AI chat during recording. Send a screenshot + instruction, get back
    a natural language response with optional structured action suggestions.
    """

    ai_keys = await _get_ai_keys(db)

    if req.context == "streaming_script":
        system_prompt = """You are an expert at writing streaming handler scripts for Writ, a web automation platform.

The script runs INSIDE the browser page context. The global `ps` object is provided by the runtime with the following APIs:

RECEIVING COMMANDS:
  ps.on("message", async ({ action, data, requestId }) => {
    // action: string, data: object from API caller, requestId: string
    // ... handle the command ...
    ps.respond(requestId, { success: true, result: someData });
  });

SENDING EVENTS (to SSE/WebSocket clients):
  ps.emit("eventName", { key: "value" });

RESPONDING TO COMMANDS:
  ps.respond(requestId, { success: true, data: {...} });

STREAMING CHUNKS (for long-running responses):
  ps.stream(requestId, { content: "partial text..." });

LOGGING:
  ps.log("debug info", someObject);

PLAYWRIGHT PAGE AUTOMATION (bridged to real Playwright — clicks, types, waits all work):
  await ps.page.click('selector');              // Click element
  await ps.page.fill('selector', 'value');      // Clear + type into input
  await ps.page.type('selector', 'text');       // Type without clearing
  await ps.page.press('Enter');                 // Press keyboard key
  await ps.page.keyboard.press('Enter');        // Same as above
  await ps.page.waitForSelector('.loaded');      // Wait for element to appear
  const text = await ps.page.textContent('.el'); // Get text content
  await ps.page.selectOption('select', 'val');   // Select dropdown option
  const result = await ps.page.evaluate(() => document.title); // Evaluate JS

DIRECT DOM ACCESS (you're already in the page context):
  document.querySelector('#myInput').value
  document.querySelectorAll('.items')
  el.click(), el.textContent, el.setAttribute(...)

MONITORING CHANGES (use setInterval polling):
  let _lastCount = 0;
  setInterval(() => {
    const msgs = document.querySelectorAll('[data-testid^="conversation-turn"]');
    if (msgs.length > _lastCount) {
      _lastCount = msgs.length;
      const latest = msgs[msgs.length - 1]?.textContent?.trim();
      ps.emit("newMessage", { text: latest, count: msgs.length });
    }
  }, 1000);

You receive a screenshot AND a DOM analysis with exact selectors. ALWAYS use the DOM data for selectors.

RULES:
- For interacting (click, fill, type): use ps.page.click/fill/type (real Playwright — handles focus, events)
- For reading data: use direct DOM access (document.querySelector)
- For monitoring changes: use setInterval + DOM queries + ps.emit()
- Always try/catch inside handlers
- Always call ps.respond(requestId, ...) to reply
- Prefer selectors: #id > [data-testid] > [aria-label] > [name] > .class
- Script must be self-contained (no imports)

When you write a script, wrap it in a ```javascript code block.
Outside the block, explain what it does. Always output the FULL script on adjustments.
If a CURRENT SCRIPT is provided below, you are EDITING it — read it first, then return the COMPLETE updated script (keep the parts you are not changing), never just the new fragment. The returned script REPLACES the current one."""
    else:
        system_prompt = """You are an AI assistant helping a user record a browser automation workflow.
The user is looking at a webpage and needs guidance on what actions to take.

When the user asks you to do something on the page, respond with:
1. A brief explanation of what needs to be done
2. A JSON array of actions under an "actions" key.

PREFER BATCH: When multiple independent actions can run without waiting for each other (e.g. filling several form fields), combine them into a single batch action. This is faster.

Action types:
- "type": "batch" — execute multiple actions at once. Contains "actions": [...] array of sub-actions.
- "type": "click" — click an element. Requires "selector".
- "type": "fill" — type into an input. Requires "selector" and "value".
- "type": "select" — select dropdown option. Requires "selector" and "value".
- "type": "scroll" — scroll the page. "value": "up" or "down".
- "type": "press" — press a key. "value": key name.
- "type": "navigate" — go to URL. "value": the URL.
- "type": "wait" — wait for element. "selector": CSS selector.
- "type": "extract" — extract data. "selector", "value" (output name).

Each action has: "type", "selector" (if applicable), "value" (if applicable), "description".

Example batch:
{"type": "batch", "description": "Fill the registration form", "actions": [
  {"type": "fill", "selector": "#email", "value": "user@example.com", "description": "Fill email"},
  {"type": "fill", "selector": "#name", "value": "John", "description": "Fill name"},
  {"type": "select", "selector": "#country", "value": "US", "description": "Select country"}
]}

If the user is just asking a question, respond with helpful text and an empty actions array.

CONTEXT YOU RECEIVE: a screenshot, the page DOM (with selectors), the steps already recorded this session, and the API calls the page has made. Use them — don't re-suggest steps already recorded, and if the user's goal maps to a captured API call, say so and suggest an "extract" of that data rather than brittle clicks.

Respond with valid JSON: {"message": "your explanation", "actions": [...]}"""

    # Multi-turn: keep more history so the conversation stays coherent.
    conversation_messages = []
    for msg in req.conversation[-10:]:
        conversation_messages.append({"role": msg.role, "content": msg.content})

    user_content = f"Current URL: {req.page_url}\n\nInstruction: {req.instruction}"
    if req.context != "streaming_script":
        user_content += f"\n\nSTEPS RECORDED SO FAR:\n{_summarize_steps(req.steps)}"
        if req.network_calls:
            user_content += f"\n\nAPI CALLS CAPTURED:\n{_summarize_network_calls(req.network_calls, max_calls=15, body_chars=200)}"
    elif req.advanced_script and req.advanced_script.strip():
        # Editing an existing streaming script → show it so the model returns a full,
        # correct rewrite rather than regenerating from scratch.
        user_content += f"\n\nCURRENT SCRIPT:\n{req.advanced_script[:40000]}"
    if req.page_dom:
        user_content += f"\n\nPAGE DOM:\n{req.page_dom[:20000]}"

    # Unified message format (Anthropic-style — gateway handles provider conversion)
    messages = [*conversation_messages, {
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.screenshot_b64}},
            {"type": "text", "text": user_content},
        ],
    }]

    try:
        ai_text, input_tokens, output_tokens, ai_model = await _call_ai(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=await ai_limits.resolve_max_tokens(db, "assist", 1500),
            purpose="assist",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_http_error(e, "AI request failed. Please try again.", action="ai_assist.chat")

    # Parse response
    message = ai_text
    actions = []
    try:
        parsed = json.loads(_strip_code_fence(ai_text))
        message = parsed.get("message", ai_text)
        actions = parsed.get("actions", [])
    except (json.JSONDecodeError, AttributeError):
        pass

    # Credit accounting
    cost_micros = _compute_cost_micros(input_tokens, output_tokens, ai_model)
    credits_used = 0  # single-user coordinator: no credit metering
    ref_id = str(uuid.uuid4())

    await db.commit()

    logger.info(
        f"[AI Assist Chat] credits={credits_used} "
        f"in={input_tokens} out={output_tokens} cost_micros={cost_micros}"
    )

    return AIChatResponse(message=message, actions=actions, credits_used=credits_used)


@router.post("/generate-extract", response_model=AIExtractResponse, dependencies=[Depends(_rate_limit_ai), Depends(require_feature("ai_workflows"))])
async def ai_generate_extract(
    req: AIExtractRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    AI-powered extraction step generation. Describe what to extract from the page,
    AI analyzes the screenshot + HTML and returns structured extraction steps.
    """

    prompt = f"""You are an expert web scraper. Look at this webpage and write a single JavaScript script that extracts the requested data.

URL: {req.page_url}

GOAL: {req.goal}

{f"WORKFLOW CONTEXT (steps already recorded that led here):{chr(10)}{_summarize_steps(req.steps, max_steps=40)}{chr(10)}" if req.steps else ""}
{f"Page HTML (truncated):{chr(10)}{req.page_html[:20000]}" if req.page_html else ""}

Write ONE JavaScript script that runs in the browser via page.evaluate() and returns a structured object with ALL the requested data.

RULES:
- Return a single JSON object with descriptive keys
- For lists/tables, return an array of objects under a key
- Always .trim() text, parseFloat() numbers, strip currency symbols
- Use robust selectors: [data-*], [role], semantic tags — avoid fragile class names
- The script is the BODY of a function — use return to return data
- ALL values must be JSON-serializable (no DOM nodes, no functions)

EXAMPLES:
- Single product: return {{ name: document.querySelector('h1')?.innerText?.trim(), price: parseFloat(document.querySelector('.price')?.innerText?.replace(/[^0-9.]/g,'')) }}
- Table: return [...document.querySelectorAll('table tbody tr')].map(r => {{ const c = r.querySelectorAll('td'); return {{ date: c[0]?.innerText?.trim(), amount: c[1]?.innerText?.trim() }} }})
- List: return {{ items: [...document.querySelectorAll('.product')].map(el => ({{ name: el.querySelector('.name')?.innerText?.trim(), price: el.querySelector('.price')?.innerText?.trim() }})) }}

Return a JSON object:
{{
  "message": "brief explanation of what the script extracts",
  "script": "the JavaScript code"
}}

Reply with ONLY the JSON, no markdown."""

    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.screenshot_b64}},
            {"type": "text", "text": prompt},
        ],
    }]

    try:
        ai_text, input_tokens, output_tokens, ai_model = await _call_ai(
            messages=messages,
            max_tokens=await ai_limits.resolve_max_tokens(db, "assist", 2000),
            purpose="assist",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_http_error(e, "AI request failed. Please try again.", action="ai_assist.extract")

    # Parse response — AI returns {"message": "...", "script": "..."}
    try:
        parsed = json.loads(_strip_code_fence(ai_text))
        script = parsed.get("script", "")
        message = parsed.get("message", "AI-generated extraction script")

        # Wrap into a single extract step
        steps = [AIExtractStep(
            selector="",
            output_name="extracted_data",
            extract_type="computed",
            description=message,
            config={"script": script},
        )]
    except (json.JSONDecodeError, AttributeError):
        logger.error(f"[AI Assist Extract] Failed to parse: {ai_text[:200]}")
        raise HTTPException(status_code=500, detail="AI returned invalid response. Please try again.")

    # Credit accounting
    cost_micros = _compute_cost_micros(input_tokens, output_tokens, ai_model)
    credits_used = 0  # single-user coordinator: no credit metering
    ref_id = str(uuid.uuid4())

    await db.commit()

    logger.info(
        f"[AI Assist Extract] credits={credits_used} "
        f"in={input_tokens} out={output_tokens} steps={len(steps)}"
    )

    return AIExtractResponse(steps=steps, message=message, credits_used=credits_used)


# --- Agentic scraper builder (drives the live browser, then writes a script) ---
# _SCRAPER_EXAMPLE is imported from services.agent_brain (above) and reused below
# by _BUILD_SCRAPER_SYSTEM.


class BuildScraperRequest(BaseModel):
    goal: str = Field(..., max_length=5000, description="What the user wants scraped")
    page_url: str = Field("", description="Current page URL")
    screenshot_b64: Optional[str] = Field(None, max_length=2_000_000, description="Current page JPEG (base64)")
    iteration: int = Field(0, description="0-based loop iteration")
    max_iterations: int = Field(14, ge=1, le=40)
    observation: Optional[dict] = Field(None, description="Latest page observation from the recorder")
    history: List[dict] = Field(default_factory=list, max_length=80, description="Prior turns: {thought, actions, results}")
    network_calls: List[dict] = Field(default_factory=list, max_length=60)


class BuildScraperResponse(BaseModel):
    thought: str = ""
    action: str = "run_actions"          # 'run_actions' | 'done'
    actions: List[dict] = Field(default_factory=list)
    script: str = ""
    variable: str = "items"
    iframe: Optional[str] = None
    summary: str = ""
    credits_used: int = 0


_BUILD_SCRAPER_SYSTEM = """You are an autonomous web-scraping agent operating a REAL browser to BUILD a reusable extraction script.

You work in a loop. Each turn you receive the current page (a screenshot plus an OBSERVATION with the URL, visible form fields, clickable buttons, and page text) and the HISTORY of actions you already ran with their results. Respond with ONE JSON object that is EITHER:

A) Explore / test — drive the browser with real actions:
   {"thought":"short reasoning","action":"run_actions","actions":[ ...action objects... ]}
B) Finish — only once you have figured out the full extraction AND verified it returns real data:
   {"thought":"...","action":"done","script":"<JS>","variable":"items","iframe":null,"summary":"one line"}

ACTION OBJECTS you may put in "actions":
- {"action":"navigate","url":"https://..."}
- {"action":"click","selector":"css"}            (real Playwright click; or use {"field_index":N}/{"button_index":N} from the observation)
- {"action":"fill","selector":"css","value":"text"}
- {"action":"select","selector":"css","value":"option"}
- {"action":"press_key","key":"Enter"}
- {"action":"scroll","direction":"down","amount":800}
- {"action":"back"}
- {"action":"wait","seconds":1.5}
- {"action":"read_text","selector":"css"}        (returns innerText)
- {"action":"evaluate_js","script":"<JS expression or async IIFE that returns JSON>"}  (returns the value — your main probing/testing tool)

HOW TO WORK:
- Use evaluate_js generously to probe the DOM and TEST candidate extraction code on a SMALL scope (first page, first ~2 rows) — it returns the real data so you can confirm selectors work before committing.
- If items need a detail view, open ONE item, learn the detail-page structure, then go back.
- Find the pagination control and confirm how "next page" works.
- You may batch a few related actions in one "actions" array to save round-trips, but each batch must finish within ~60s. NEVER run a full multi-page scrape during exploration.
- Keep going until you are confident; you have a limited number of iterations.

FINAL "done" SCRIPT REQUIREMENTS:
- A single async IIFE that RETURNS a JSON-serializable object: (async () => { ...; return { total, <variable> }; })()
- It must do the WHOLE job autonomously: read the list; if needed click into each item, read detail fields, go back; paginate until there is no next page; accumulate everything.
- It runs via page.evaluate() with no external state — resolve its own document/iframe, don't rely on anything from your exploration turns.
- Robust selectors; .trim() text; parseFloat() numbers; small awaits after clicks/navigation.
- "variable" = the output key name (e.g. "products"). Set "iframe" to a substring of the iframe src if the data lives inside an iframe, otherwise null.

EXAMPLE of a good final script (list + per-row detail drill-down + pagination inside an iframe):
""" + _SCRAPER_EXAMPLE + """

Reply with ONLY the JSON object — no markdown, no commentary outside the JSON."""


# _loads_lenient / _strip_code_fence / _summarize_scraper_history are imported
# from services.agent_brain (above) and used across the endpoints below.


@router.post("/build-scraper", response_model=BuildScraperResponse, dependencies=[Depends(_rate_limit_ai), Depends(require_feature("ai_workflows"))])
async def ai_build_scraper(
    req: BuildScraperRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """One turn of the agentic scraper builder. The model decides whether to drive
    the browser further (run_actions) or to finalize the reusable script (done).
    The frontend executes returned actions via the recorder's ephemeral agent_action
    path (which never records workflow steps) and loops back with the results.
    """

    obs = req.observation or {}
    obs_text = ""
    if obs:
        try:
            obs_text = json.dumps({
                "current_url": obs.get("current_url"),
                "fields": obs.get("fields", [])[:30],
                "buttons": obs.get("buttons", [])[:30],
                "page_text": str(obs.get("page_text", ""))[:2500],
                "errors": obs.get("errors", [])[:8],
            })[:6000]
        except (TypeError, ValueError):
            obs_text = str(obs)[:6000]

    user_text = (
        f"GOAL: {req.goal}\n\n"
        f"Current URL: {req.page_url}\n"
        f"Iteration: {req.iteration + 1} of {req.max_iterations}"
        f"{' — running low, wrap up soon and finalize the script if you can.' if req.iteration >= req.max_iterations - 3 else ''}\n\n"
        f"PAGE OBSERVATION:\n{obs_text or '  (none yet — request actions or evaluate_js to inspect the page)'}\n\n"
        f"ACTION HISTORY:\n{_summarize_scraper_history(req.history)}\n\n"
        f"CAPTURED API CALLS (a matching JSON endpoint may let you skip UI scraping):\n{_summarize_network_calls(req.network_calls, max_calls=15)}\n\n"
        "Decide the next step. Probe/test with evaluate_js before finalizing. "
        "Return the JSON object now."
    )

    content: list = []
    if req.screenshot_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.screenshot_b64}})
    content.append({"type": "text", "text": user_text})
    messages = [{"role": "user", "content": content}]

    try:
        ai_text, input_tokens, output_tokens, ai_model = await _call_ai(
            messages=messages,
            system_prompt=_BUILD_SCRAPER_SYSTEM,
            max_tokens=await ai_limits.resolve_max_tokens(db, "agent", 3000),
            purpose="assist",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_http_error(e, "AI request failed. Please try again.", action="ai_assist.build_scraper")

    try:
        parsed = _loads_lenient(ai_text)
    except (json.JSONDecodeError, AttributeError):
        logger.error(f"[AI Build Scraper] Failed to parse: {ai_text[:300]}")
        raise HTTPException(status_code=500, detail="AI returned invalid response. Please try again.")

    action = parsed.get("action") or ("done" if parsed.get("script") else "run_actions")
    raw_actions = parsed.get("actions") or []
    actions = [a for a in raw_actions if isinstance(a, dict)][:25]

    # Credit accounting (mirrors the other assist endpoints).
    cost_micros = _compute_cost_micros(input_tokens, output_tokens, ai_model)
    credits_used = 0  # single-user coordinator: no credit metering
    ref_id = str(uuid.uuid4())
    await db.commit()

    logger.info(
        f"[AI Build Scraper] iter={req.iteration} action={action} "
        f"actions={len(actions)} credits={credits_used} in={input_tokens} out={output_tokens}"
    )

    return BuildScraperResponse(
        thought=str(parsed.get("thought", ""))[:2000],
        action="done" if action == "done" else "run_actions",
        actions=actions,
        script=str(parsed.get("script", "")),
        variable=str(parsed.get("variable") or "items")[:60],
        iframe=(parsed.get("iframe") or None),
        summary=str(parsed.get("summary", ""))[:500],
        credits_used=credits_used,
    )


# --- Unified mode-aware agent (the always-open AI chat drives the browser) ---
# The agent prompts (AGENT_BASE / AGENT_MODE_PROMPTS) and the per-turn decision
# logic now live in services.agent_brain and are shared with the autonomous
# orchestrator — see run_agent_turn() below.


class AgentChatRequest(BaseModel):
    instruction: str = Field(..., max_length=5000, description="The user's latest message")
    mode: str = Field("manual", description="manual | api | streaming")
    conversation: List[AIChatMessage] = Field(default_factory=list, max_length=20)
    page_url: str = Field("", description="Current page URL")
    screenshot_b64: Optional[str] = Field(None, max_length=2_000_000)
    observation: Optional[dict] = Field(None, description="Latest page observation from the recorder")
    steps: List[dict] = Field(default_factory=list, max_length=500, description="Steps recorded so far")
    network_calls: List[dict] = Field(default_factory=list, max_length=60)
    iteration: int = Field(0)
    max_iterations: int = Field(12, ge=1, le=40)
    history: List[dict] = Field(default_factory=list, max_length=80, description="Prior agent turns this task")
    conversation_id: Optional[str] = Field(None, max_length=200, description="Stable id for this chat → routes to its own streaming tab")
    # Streaming mode: the current advanced script so the agent can SEE and EDIT it.
    advanced_script: str = Field("", max_length=200_000, description="Current streaming advanced script, if any")
    advanced_enabled: bool = Field(False, description="Whether the advanced script is enabled (informational)")


class AgentChatResponse(BaseModel):
    thought: str = ""
    action: str = "ask"                  # ask | run_actions | done
    message: str = ""
    actions: List[dict] = Field(default_factory=list)
    steps_to_add: List[dict] = Field(default_factory=list)
    # Edits to EXISTING steps (update/delete/move); the frontend previews then applies.
    step_edits: List[dict] = Field(default_factory=list)
    script: str = ""
    # "append" (default) or "replace" — how the returned script updates the current one.
    script_mode: str = "append"
    handler_name: str = ""
    variable: str = ""
    iframe: Optional[str] = None
    summary: str = ""
    credits_used: int = 0


@router.post("/agent", response_model=AgentChatResponse, dependencies=[Depends(_rate_limit_ai), Depends(require_feature("ai_workflows"))])
async def ai_agent_turn(
    req: AgentChatRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """One turn of the unified, mode-aware AI agent that powers the always-open
    recorder chat. The model decides whether to just answer (ask), drive the live
    browser (run_actions — executed via the recorder's ephemeral agent_action path,
    never recorded), or finalize the artifact for the current mode (done). The
    frontend previews any returned steps/script for confirmation before adding."""

    # Commit/close the read txn before the long gateway call below so a streaming
    # provider routed back into THIS coordinator doesn't contend on the same conn.
    await db.commit()

    # Normalize the mode once for logging (the brain normalizes again internally —
    # same logic — so the two never drift).
    mode, _ = agent_brain.build_system_prompt(req.mode)

    # The entire decision (prompt assembly, gateway call, lenient JSON parse with the
    # retry-on-unparseable envelope, action/field coercion) lives in the shared brain
    # so this endpoint and the autonomous orchestrator stay identical. call_ai raises
    # HTTPException on transport error; let it propagate as before.
    turn = await agent_brain.run_agent_turn(
        mode=req.mode,
        instruction=req.instruction,
        conversation=req.conversation,
        page_url=req.page_url,
        observation=req.observation,
        steps=req.steps,
        history=req.history,
        network_calls=req.network_calls,
        iteration=req.iteration,
        max_iterations=req.max_iterations,
        advanced_script=req.advanced_script,
        screenshot_b64=req.screenshot_b64,
        conversation_id=req.conversation_id,
        max_tokens=await ai_limits.resolve_max_tokens(db, "agent", 3000),
    )

    # Unparseable-reply retry envelope: no credits charged, no DB write (matches the
    # prior behavior of returning action="retry" with credits_used=0 before billing).
    if turn.action == "retry":
        return AgentChatResponse(
            action="retry",
            thought=turn.thought,
            message=turn.message,
            credits_used=0,
        )

    cost_micros = _compute_cost_micros(turn.input_tokens, turn.output_tokens, turn.model)
    credits_used = 0  # single-user coordinator: no credit metering
    ref_id = str(uuid.uuid4())
    await db.commit()

    logger.info(
        f"[AI Agent] mode={mode} iter={req.iteration} action={turn.action} "
        f"actions={len(turn.actions)} steps={len(turn.steps_to_add)} credits={credits_used}"
    )

    return AgentChatResponse(
        thought=turn.thought,
        action=turn.action,
        message=turn.message,
        actions=turn.actions,
        steps_to_add=turn.steps_to_add,
        step_edits=turn.step_edits,
        script=turn.script,
        script_mode=turn.script_mode,
        handler_name=turn.handler_name,
        variable=turn.variable,
        iframe=turn.iframe,
        summary=turn.summary,
        credits_used=credits_used,
    )


# --- Generate Streaming Script ---

class AIStreamingScriptRequest(BaseModel):
    screenshot_b64: str = Field(..., max_length=2_000_000, description="JPEG screenshot of current page as base64")
    page_url: str = Field(..., description="Current page URL")
    goal: str = Field(..., max_length=5000, description="What the streaming script should do")
    existing_handlers: List[str] = Field(default_factory=list, description="Names of already-defined handlers for context")
    page_dom: Optional[str] = Field(None, max_length=30000, description="DOM/selectors of the current page")
    network_calls: List[dict] = Field(default_factory=list, max_length=60, description="API calls the page makes (for emit-on-response / data polling)")


class AIStreamingScriptResponse(BaseModel):
    script: str = ""
    message: str = ""
    credits_used: int = 0


@router.post("/generate-streaming-script", response_model=AIStreamingScriptResponse, dependencies=[Depends(_rate_limit_ai), Depends(require_feature("ai_workflows"))])
async def ai_generate_streaming_script(
    req: AIStreamingScriptRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    AI-powered streaming handler script generation. Describe what the script should do,
    AI sees the page screenshot and generates a persistent script for the streaming session.
    """

    handlers_ctx = ""
    if req.existing_handlers:
        handlers_ctx = f"\nAlready defined handlers: {', '.join(req.existing_handlers)}\nYour script can call these handlers or complement them.\n"

    prompt = f"""You are an expert at writing Playwright-based streaming handler scripts for a web automation platform called Writ.

The script runs inside a persistent browser session. It receives API commands and interacts with the current page using the `ps` (PageSession) helper object.

PAGE CONTEXT:
- URL: {req.page_url}
- Look at the screenshot to understand the current page structure and visible elements.
{handlers_ctx}
{f"PAGE DOM (use these REAL selectors, do not invent):{chr(10)}{req.page_dom[:15000]}{chr(10)}" if req.page_dom else ""}
{f"API CALLS THIS PAGE MAKES (prefer emitting/reading from these over scraping the DOM when relevant):{chr(10)}{_summarize_network_calls(req.network_calls, max_calls=15, body_chars=250)}{chr(10)}" if req.network_calls else ""}
USER GOAL: {req.goal}

AVAILABLE APIs in the script:

1. Event handler — respond to incoming API requests:
   ps.on("message", async ({{ action, data, requestId }}) => {{
     // action: string — the command name from the API caller
     // data: object — payload from the API caller
     // Execute Playwright actions on the page:
     const page = ps.page;  // Playwright Page object
     // Use page.click(), page.fill(), page.evaluate(), page.waitForSelector(), etc.
     // Return response to the API caller:
     ps.respond(requestId, {{ success: true, result: someData }});
   }});

1b. NAMED callable functions (preferred when the goal has DISTINCT operations) —
    register one handler PER function name. The incoming action routes to the
    matching named handler; ps.on("message", ...) stays as the catch-all fallback.
    These named functions become first-class callable workflow functions (callable
    via the Managed-API / MCP / per-buyer keys as action=<function name>):
   ps.on("get_user", async ({{ data, requestId }}) => {{
     const user = await ps.page.evaluate(() => ({{ /* ...extract... */ }}));
     ps.respond(requestId, {{ success: true, result: user }});  // shape = declared output_fields
   }});
   ps.fn("search", async ({{ data, requestId }}) => {{   // ps.fn is an alias for ps.on
     ps.respond(requestId, {{ success: true, result: [] }});
   }});
   // Prefer named functions over one big switch(action) inside ps.on("message").

2. Emit events to connected SSE/WebSocket clients:
   ps.emit("event_name", {{ key: "value" }});

3. Access the Playwright page directly:
   const page = ps.page;
   await page.click('selector');
   await page.fill('input', 'value');
   const text = await page.evaluate(() => document.querySelector('h1')?.innerText);
   await page.waitForSelector('.loaded');

4. Scheduled/interval tasks:
   setInterval(async () => {{
     const data = await ps.page.evaluate(() => ({{ /* extract */ }}));
     ps.emit("update", data);
   }}, 30000);

RULES:
- Write clean, async-safe JavaScript
- Always use try/catch inside handlers for robustness
- Use ps.respond(requestId, data) to reply to API callers
- Use ps.emit(event, data) to broadcast to SSE/WS clients
- Access page via ps.page (Playwright Page object)
- For data extraction, use page.evaluate() with DOM queries
- Return the script as plain JavaScript, no markdown fences
- The script must be self-contained — no imports needed

Return a JSON object:
{{
  "message": "brief explanation of what the script does",
  "script": "the JavaScript code"
}}

Reply with ONLY the JSON, no markdown."""

    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.screenshot_b64}},
            {"type": "text", "text": prompt},
        ],
    }]

    try:
        ai_text, input_tokens, output_tokens, ai_model = await _call_ai(
            messages=messages,
            max_tokens=await ai_limits.resolve_max_tokens(db, "agent", 3000),
            purpose="assist",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_http_error(e, "AI request failed. Please try again.", action="ai_assist.streaming_script")

    try:
        parsed = json.loads(_strip_code_fence(ai_text))
        script = parsed.get("script", "")
        message = parsed.get("message", "AI-generated streaming script")
    except (json.JSONDecodeError, AttributeError):
        logger.error(f"[AI Assist StreamingScript] Failed to parse: {ai_text[:200]}")
        raise HTTPException(status_code=500, detail="AI returned invalid response. Please try again.")

    cost_micros = _compute_cost_micros(input_tokens, output_tokens, ai_model)
    credits_used = 0  # single-user coordinator: no credit metering
    ref_id = str(uuid.uuid4())

    await db.commit()

    logger.info(
        f"[AI Assist StreamingScript] credits={credits_used} "
        f"in={input_tokens} out={output_tokens}"
    )

    return AIStreamingScriptResponse(script=script, message=message, credits_used=credits_used)


# --- Optimize Workflow ---

class OptimizeRequest(BaseModel):
    steps: List[dict] = Field(..., description="Recorded workflow steps")
    screenshot_b64: Optional[str] = Field(None, description="Final page screenshot for context")
    page_url: Optional[str] = Field(None, description="Final page URL")
    # Captured network traffic during recording — lets the AI replace UI
    # fill+submit sequences with a single api_call step when a matching request exists.
    network_calls: List[dict] = Field(default_factory=list, max_length=60, description="Captured API requests/responses")
    # Non-sensitive form values typed during recording (for {{placeholder}} parameterization).
    form_data: dict = Field(default_factory=dict)
    # Names of detected credentials (values NEVER sent) → {{secret:name}} placeholders.
    credential_keys: List[str] = Field(default_factory=list)


class OptimizeChange(BaseModel):
    action: str = "removed"           # removed | replaced | reordered | added
    step_indices: List[int] = Field(default_factory=list)  # original indices affected
    description: str = ""             # human-readable summary
    reason: str = ""                 # why it's safe / what it does
    risk: str = "safe"               # safe | caution | high


class OptimizeResponse(BaseModel):
    steps: List[dict] = Field(default_factory=list)
    removed_count: int = 0
    # Structured changes (frontend renders a reviewable diff with risk badges).
    changes: List[OptimizeChange] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    credits_used: int = 0


@router.post("/optimize-workflow", response_model=OptimizeResponse, dependencies=[Depends(_rate_limit_ai), Depends(require_feature("ai_workflows"))])
async def ai_optimize_workflow(
    req: OptimizeRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    AI analyzes recorded steps, removes unnecessary ones, fixes ordering,
    and returns an optimized workflow.
    """

    steps_json = json.dumps(req.steps, indent=2, default=str)
    network_summary = _summarize_network_calls(req.network_calls)
    form_keys = list(req.form_data.keys()) if req.form_data else []

    prompt = f"""You are an expert browser-automation engineer. Optimize this recorded workflow WITHOUT breaking it.

You get the recorded UI steps AND the API calls the page actually made during recording. Two kinds of optimization:

1) API SUBSTITUTION (biggest win) — When a sequence of UI steps (e.g. fill several fields then click submit, or a login) caused a captured API call that achieves the same result, REPLACE that whole UI sequence with ONE api_call step. Shape:
   {{"id": "api_<n>", "type": "api_call", "enabled": true, "config": {{
       "function_name": "snake_case_name", "method": "POST", "url": "<exact captured url>",
       "headers": {{...captured request headers...}},
       "body_template": {{...captured request body, but with placeholders...}},
       "response_extractions": {{"token": "$.data.token"}},  // JSONPath into the captured response
       "timeout_ms": 30000 }} }}
   - Parameterize values: a value equal to a recorded form value → "{{{{key}}}}" (keys: {form_keys}); a credential value → "{{{{secret:name}}}}" (names: {req.credential_keys}). Never inline real passwords.
   - Only substitute when a captured call clearly corresponds (URL + method + the form values appear in its request body). If unsure, DO NOT substitute.

2) PRUNING — remove only steps that are provably safe to drop:
   - exact duplicate clicks/scrolls, redundant consecutive scrolls, a wait immediately before a navigate (navigate already waits), hover/mousemove artifacts not followed by an action.

HARD SAFETY RULES (never break the flow):
- NEVER remove a fill/select whose value is later used or submitted, unless it's folded into an api_call replacement.
- NEVER remove navigate / navigated_to (they establish page + cookies/session), extract, or return steps.
- NEVER reorder across a navigate.
- NEVER change selectors/values on steps you keep; preserve their ids and all fields.
- A login/auth sequence may ONLY be replaced by an api_call if a matching auth request was captured; otherwise keep it untouched.
- When unsure, KEEP the step and note it in warnings. Correctness beats brevity.

RECORDED STEPS:
{steps_json}

CAPTURED API CALLS (method url -> status; bodies truncated):
{network_summary}

{f"Final URL: {req.page_url}" if req.page_url else ""}

Return ONLY a JSON object:
{{
  "steps": [ ...the full optimized step array (kept steps unchanged + any new api_call steps), in execution order... ],
  "removed_count": <int>,
  "changes": [ {{"action": "removed|replaced|reordered|added", "step_indices": [<original indices affected>], "description": "<what changed>", "reason": "<why it's safe / what the api_call does>", "risk": "safe|caution|high"}} ],
  "warnings": [ "<anything you were unsure about and left as-is>" ]
}}
Mark every api_call substitution as risk "caution" (the user will review). Pruning of exact duplicates is "safe". If nothing is safe to change, return the steps unchanged with removed_count 0 and empty changes."""

    # Unified message format (Anthropic-style — gateway handles provider conversion)
    messages = [{
        "role": "user",
        "content": ([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.screenshot_b64}},
                      {"type": "text", "text": prompt}]
                     if req.screenshot_b64 else prompt),
    }]

    try:
        ai_text, input_tokens, output_tokens, ai_model = await _call_ai(
            messages=messages,
            max_tokens=await ai_limits.resolve_max_tokens(db, "optimize", 6000),
            purpose="assist",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_http_error(e, "AI request failed. Please try again.", action="ai_assist.optimize")

    # Graceful: if the model returns something unparseable, return the original
    # workflow untouched with a warning rather than failing the request.
    optimized_steps = req.steps
    removed_count = 0
    changes: list = []
    warnings: list = []
    try:
        parsed = json.loads(_strip_code_fence(ai_text))
        if isinstance(parsed.get("steps"), list) and parsed["steps"]:
            optimized_steps = parsed["steps"]
        removed_count = int(parsed.get("removed_count", 0) or 0)
        raw_changes = parsed.get("changes", []) or []
        for c in raw_changes:
            if isinstance(c, dict):
                changes.append(OptimizeChange(
                    action=str(c.get("action", "removed")),
                    step_indices=[int(i) for i in c.get("step_indices", []) if isinstance(i, (int, float))],
                    description=str(c.get("description", "")),
                    reason=str(c.get("reason", "")),
                    risk=str(c.get("risk", "safe")),
                ))
            elif isinstance(c, str):
                changes.append(OptimizeChange(description=c))
        warnings = [str(w) for w in (parsed.get("warnings", []) or [])]
    except (json.JSONDecodeError, AttributeError, ValueError, TypeError):
        logger.warning("[AI Optimize] Unparseable response — returning workflow unchanged")
        warnings = ["The optimizer returned an unexpected response, so no changes were applied. Try again."]

    cost_micros = _compute_cost_micros(input_tokens, output_tokens, ai_model)
    credits_used = 0  # single-user coordinator: no credit metering

    await db.commit()

    logger.info(
        f"[AI Optimize] removed={removed_count} "
        f"changes={len(changes)} credits={credits_used}"
    )

    return OptimizeResponse(
        steps=optimized_steps,
        removed_count=removed_count,
        changes=changes,
        warnings=warnings,
        credits_used=credits_used,
    )


class OptimizeLiveRequest(BaseModel):
    workflow_id: int
    confirm_side_effects: bool = False


def _optimize_live_unavailable_envelope(warning: str) -> dict:
    """The honest empty diff returned when no fleet agent can optimize (never a 500). Same shape the FE
    confirm UI renders (`services.workflow_optimizer_live._envelope`), so a "nothing changed" result and
    a real diff are indistinguishable to the caller."""
    return {
        "steps": [],
        "changes": [],
        "warnings": [warning],
        "removed_count": 0,
        "requires_confirm": False,
        "verified": False,
        "credits_used": 0,
    }


@router.post("/optimize-workflow-live")
async def ai_optimize_workflow_live(
    req: OptimizeLiveRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Live workflow optimizer (self-host): replay the workflow on the fleet agent that holds its mirror
    WITH network capture, then propose (and live-verify) DOM->api_call/login_post substitutions +
    pruning. Returns `{steps, changes, warnings, removed_count, requires_confirm, verified}` — same shape
    as the static optimizer plus the live gate/verified flags. Returns the diff only; the FE Applies via
    PUT.

    This is a PROXY: the daemon owns the recipe + secrets, so we resolve the fleet agent + daemon-side
    `local_id` for this coordinator `AutomationWorkflow`, dispatch an `optimize_workflow_live` frame, and
    await the finished diff. Only `{local_id, confirm_side_effects}` cross the wire; the returned steps
    carry `{{placeholders}}` (never plaintext). When no online fleet agent holds this workflow, we return
    the honest empty-diff envelope (never a 500). The Python `services.workflow_optimizer_live` stays as
    an unused fallback."""
    from models.local_workflow import LocalWorkflow
    from routers.user_recorder_ws import send_and_await, _connections

    # Resolve the fleet agent + daemon-side local_id from the LocalWorkflow mirror stamped at deploy
    # time (source_workflow_id -> this coordinator AutomationWorkflow.id). Prefer a mirror whose agent is
    # currently connected so an offline-but-mirrored copy doesn't win over a live one.
    mirrors = (
        await db.execute(
            select(LocalWorkflow).where(
                LocalWorkflow.source_workflow_id == req.workflow_id,
                LocalWorkflow.status == "active",
            )
        )
    ).scalars().all()
    if not mirrors:
        return _optimize_live_unavailable_envelope(
            "No fleet agent is available to optimize this workflow."
        )
    target = next((m for m in mirrors if m.agent_id in _connections), None)
    if target is None:
        return _optimize_live_unavailable_envelope(
            "The fleet agent holding this workflow is offline — reconnect it and try again."
        )

    frame = {
        "type": "optimize_workflow_live",
        "request_id": str(uuid.uuid4()),
        "local_id": str(target.local_id),
        "confirm_side_effects": req.confirm_side_effects,
    }
    # The replay (30-120s) + AI round-trip runs entirely on the agent; wait past the FE's 180s HTTP
    # timeout headroom. On disconnect/timeout send_and_await returns an {'error': ...} dict.
    reply = await send_and_await(
        target.agent_id,
        frame,
        reply_type="optimize_workflow_live_result",
        correlate_by="request_id",
        timeout=180,
    )
    if not reply or reply.get("error"):
        detail = (reply or {}).get("error") or "no reply"
        return _optimize_live_unavailable_envelope(
            f"The fleet agent could not optimize this workflow ({detail})."
        )

    # The daemon splats the full diff envelope alongside the frame fields; return them as the response.
    return {
        "steps": reply.get("steps", []),
        "changes": reply.get("changes", []),
        "warnings": reply.get("warnings", []),
        "removed_count": reply.get("removed_count", 0),
        "requires_confirm": reply.get("requires_confirm", False),
        "verified": reply.get("verified", False),
        "credits_used": reply.get("credits_used", 0),
    }


# --- Workflow Segment Detection (code-based, no AI) ---

class DetectSegmentsRequest(BaseModel):
    steps: List[dict] = Field(..., description="Recorded workflow steps")


class WorkflowSegment(BaseModel):
    name: str
    segment_type: str  # "auth", "navigation", "extraction"
    step_indices: List[int]
    depends_on: List[str] = Field(default_factory=list)
    extract_outputs: List[str] = Field(default_factory=list)


class DetectSegmentsResponse(BaseModel):
    segments: List[WorkflowSegment] = Field(default_factory=list)
    has_auth: bool = False


@router.post("/detect-segments", response_model=DetectSegmentsResponse, dependencies=[Depends(_rate_limit_ai)])
async def detect_workflow_segments(
    req: DetectSegmentsRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Code-based segment detection. No AI, no credits.
    Analyzes step patterns to find auth prefix and per-extract navigation groups.
    """
    steps = req.steps
    if not steps:
        return DetectSegmentsResponse(segments=[], has_auth=False)

    # 1. Find all extract step indices and return step
    extract_indices = []
    for i, s in enumerate(steps):
        if s.get("type") == "extract":
            extract_indices.append(i)

    if not extract_indices:
        return DetectSegmentsResponse(segments=[], has_auth=False)

    # 2. Detect auth segment: steps before first non-navigate/non-fill/non-click
    #    that look like login (fill password, sensitive fields, login URL patterns)
    auth_end = 0
    auth_signals = 0
    LOGIN_URL_HINTS = ["login", "signin", "sign-in", "auth", "sso", "oauth", "account"]

    for i, s in enumerate(steps):
        step_type = s.get("type", "")
        options = s.get("options", {}) or {}
        url = (s.get("url") or "").lower()
        selector = (s.get("selector") or "").lower()
        description = (s.get("description") or "").lower()

        # URL-based login detection
        if step_type == "navigate" and any(h in url for h in LOGIN_URL_HINTS):
            auth_signals += 2
            auth_end = i + 1

        # Sensitive field = almost certainly auth
        if options.get("is_sensitive") or options.get("field_type") == "password":
            auth_signals += 3
            auth_end = i + 1

        # Fill into a password/email-looking field
        if step_type == "fill" and any(kw in selector for kw in ["password", "passwd", "login", "email", "user"]):
            auth_signals += 2
            auth_end = i + 1

        # Submit/login button click
        if step_type == "click" and any(kw in description + selector for kw in ["login", "sign in", "submit", "log in"]):
            auth_signals += 2
            auth_end = i + 1

        # Stop looking after first extract
        if step_type == "extract":
            break

    # Extend auth_end to include any wait/navigate right after login click
    while auth_end < len(steps) and auth_end < (extract_indices[0] if extract_indices else len(steps)):
        s = steps[auth_end]
        if s.get("type") in ("wait", "navigated_to") or (
            s.get("type") == "navigate" and not any(h in (s.get("url") or "").lower() for h in LOGIN_URL_HINTS)
        ):
            auth_end += 1
            break
        break

    has_auth = auth_signals >= 3
    auth_segment_indices = list(range(0, auth_end)) if has_auth else []

    # 3. Group extract steps and their preceding navigation
    #    Walk backwards from each extract group to find its navigation path
    extract_groups = []
    i = 0
    while i < len(extract_indices):
        group_start = extract_indices[i]
        group_end = group_start
        # Collect consecutive extract steps
        while i < len(extract_indices) - 1 and extract_indices[i + 1] == extract_indices[i] + 1:
            i += 1
            group_end = extract_indices[i]
        # Also include a following return step
        if group_end + 1 < len(steps) and steps[group_end + 1].get("type") == "return":
            group_end += 1
        extract_groups.append((group_start, group_end))
        i += 1

    # 4. For each extract group, find its navigation steps (between auth_end and group_start)
    segments = []

    if has_auth:
        segments.append(WorkflowSegment(
            name="login",
            segment_type="auth",
            step_indices=auth_segment_indices,
            depends_on=[],
            extract_outputs=[],
        ))

    nav_start = auth_end if has_auth else 0

    for g_idx, (g_start, g_end) in enumerate(extract_groups):
        # Navigation steps = steps between end of previous group (or auth) and this extract group
        nav_indices = list(range(nav_start, g_start))

        # Build a name from extract output_names
        extract_outputs = []
        for si in range(g_start, g_end + 1):
            s = steps[si]
            if s.get("type") == "extract":
                out_name = (s.get("options") or {}).get("output_name", "")
                if out_name:
                    extract_outputs.append(out_name)

        # Derive segment name from navigation URLs or extract outputs
        seg_name = ""
        for ni in nav_indices:
            ns = steps[ni]
            if ns.get("type") in ("click",) and ns.get("description"):
                # Use the last meaningful click description
                desc = ns["description"]
                for prefix in ["Click ", "click "]:
                    desc = desc.removeprefix(prefix)
                seg_name = desc.strip().strip('"').strip("'")[:40]

        if not seg_name and extract_outputs:
            seg_name = extract_outputs[0].replace("_", " ").title()
        if not seg_name:
            seg_name = f"extraction_{g_idx + 1}"

        all_indices = nav_indices + list(range(g_start, g_end + 1))
        depends = ["login"] if has_auth else []

        segments.append(WorkflowSegment(
            name=seg_name,
            segment_type="extraction",
            step_indices=all_indices,
            depends_on=depends,
            extract_outputs=extract_outputs,
        ))

        # Next group's nav starts after this group
        nav_start = g_end + 1

    return DetectSegmentsResponse(segments=segments, has_auth=has_auth)


# === AI Selector Finder ===

class FindSelectorsRequest(BaseModel):
    url: str
    prompt: str = Field(..., description="What the user wants to monitor (e.g., 'watch the price and stock status')")
    screenshot_b64: str = Field(default="", description="Current page screenshot as base64 JPEG")
    page_dom: str = Field(default="", description="Current page DOM HTML (truncated)")
    existing_selectors: List[str] = Field(default_factory=list)


class FoundSelector(BaseModel):
    selector: str
    name: str
    description: str


class FindSelectorsResponse(BaseModel):
    selectors: List[FoundSelector]
    credits_used: int = 1


@router.post("/find-selectors", response_model=FindSelectorsResponse, dependencies=[Depends(_rate_limit_ai), Depends(require_feature("ai_workflows"))])
async def find_selectors(
    req: FindSelectorsRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    AI finds CSS selectors based on what the user wants to monitor.
    Uses the current screenshot + DOM from the recorder preview.
    Costs 1 credit.
    """
    if not auth.user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Enforce credits

    system_prompt = """You are a CSS selector expert. The user wants to monitor specific content on a webpage.
Given the page screenshot and/or DOM, find the best CSS selectors for the elements the user described.

Rules:
- Return 1-5 selectors, each targeting a specific piece of content
- GROUNDING: when a Page DOM is provided, ONLY return selectors that actually appear in that DOM — never invent class names, ids, or attributes you can't see. If you can't find a real selector for something, omit it rather than guessing.
- Prefer stable selectors: IDs > data attributes > aria-labels > unique classes > nth-of-type
- Avoid fragile selectors (deep nesting, index-based without context)
- Each selector should target a single meaningful element (not a container with lots of children)
- Name each selector clearly (e.g., "Product Price", "Stock Status", "Rating")

Respond with valid JSON only:
{"selectors": [{"selector": "CSS selector", "name": "human name", "description": "what this captures"}]}"""

    user_content = f"URL: {req.url}\n\nUser wants to monitor: {req.prompt}"
    if req.existing_selectors:
        user_content += f"\n\nAlready selected (skip these): {', '.join(req.existing_selectors)}"
    if req.page_dom:
        # Truncate DOM to fit context
        dom_preview = req.page_dom[:30000]
        user_content += f"\n\nPage DOM (truncated):\n{dom_preview}"

    # Unified message format (Anthropic-style — gateway handles provider conversion)
    messages = [{
        "role": "user",
        "content": ([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": req.screenshot_b64}},
                      {"type": "text", "text": user_content}]
                     if req.screenshot_b64 else user_content),
    }]

    try:
        text, input_tokens, output_tokens, ai_model = await _call_ai(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=await ai_limits.resolve_max_tokens(db, "assist", 2000),
            purpose="assist",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_http_error(e, "AI request failed. Please try again.", status_code=502, action="ai_assist.find_selectors")

    # Parse response
    selectors = []
    try:
        # Extract JSON from response (may have markdown wrapper)
        import re
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            parsed = json.loads(json_match.group())
            for s in parsed.get("selectors", []):
                if s.get("selector") and s["selector"] not in req.existing_selectors:
                    selectors.append(FoundSelector(
                        selector=s["selector"],
                        name=s.get("name", s["selector"]),
                        description=s.get("description", ""),
                    ))
    except Exception as e:
        logger.warning(f"Failed to parse AI selector response: {e}, raw: {text[:200]}")

    # Credit accounting
    cost_micros = _compute_cost_micros(input_tokens, output_tokens, ai_model)
    credits = 0  # single-user coordinator: no credit metering
    ref_id = str(uuid.uuid4())

    await db.commit()

    return FindSelectorsResponse(selectors=selectors, credits_used=credits)


# ── Generate a full automation from a goal ───────────────────────────────────
# Turns a natural-language goal into a complete AutomationSpec (block tree) the
# flow builder plays into the canvas. The block vocabulary + the user's resources
# are injected by the CLIENT (catalog_digest + resource_context) so there is one
# source of truth for "what blocks exist" (frontend components/flows/blockCatalog).
# Keep AUTOMATION_SYSTEM byte-aligned with the desktop daemon prompt
# (the agent's automation-generation system prompt).

AUTOMATION_SYSTEM = """You are an automation architect. Turn the user's goal into ONE complete automation, expressed as a JSON "AutomationSpec".

An automation is a TREE of blocks:
- exactly ONE root EVENT block (how it starts) — no parentId,
- optional CONDITION blocks that gate the flow on an upstream value,
- ACTION blocks that do things,
each linked to its parent by `parentId`.

Reply with ONLY this JSON (no markdown, no prose):
{
  "name": "short title",
  "description": "one sentence",
  "blocks": [
    { "id": "b1", "type": "event|condition|action", "blockType": "<from catalog>", "config": { ... } },
    { "id": "b2", "type": "action", "blockType": "...", "config": { ... }, "parentId": "b1" }
  ],
  "rationale": "one sentence on why it is shaped this way",
  "block_notes": { "b1": "present-tense line describing this block", "b2": "..." },
  "unresolved": [ { "blockId": "b2", "field": "recipients", "kind": "recipient|selector|value|persona|file|workflow|confirm", "question": "...", "options": [ { "id": 12, "label": "..." } ], "multi": false } ],
  "new_resources": [ { "kind": "monitor|workflow", "blockId": "b1", "suggestion": "..." } ],
  "requires_cloud": false
}

RULES:
- Use ONLY blockTypes from BLOCK CATALOG below. Exactly one root event block with no parentId.
- Reference the user's EXISTING items by their real numeric id from RESOURCES (workflow_id, target_id, persona_id, ai_session_id). NEVER invent an id.
- If the goal needs a resource that is NOT in RESOURCES: for a new page to watch, use change_detected with a `url` and add a `new_resources` entry; otherwise add an `unresolved` item asking the user to pick.
- Put anything you cannot fill (a recipient to notify, a selector, a specific value) into `unresolved` and leave that config field empty.
- When several RESOURCES could fit an unresolved slot, offer a short `options` list (each { id, label } from RESOURCES) so the user picks one inline; set `multi` true when more than one may be chosen (e.g. recipients).
- Prefer the FEWEST blocks that achieve the goal. Set `requires_cloud` true only if you used a cloud-only block.
- `block_notes` gives one short line per block id, used to narrate the build.
- If a CURRENT AUTOMATION section is provided below, you are EDITING that automation — do NOT rebuild it and do NOT return "blocks". Instead return an "edits" array of operations on it, referencing the existing block ids shown. Each op is one of:
    {"op":"add","block":{"id":"n1","type":"action","blockType":"...","config":{...},"parentId":"<existing or earlier-added id>"},"note":"..."}
    {"op":"remove","blockId":"<existing id>","note":"..."}
    {"op":"move","blockId":"<existing id>","parentId":"<existing id>","note":"..."}
    {"op":"update","blockId":"<existing id>","config":{...},"note":"..."}
    {"op":"set_meta","name":"...","description":"..."}
  Edit rules: never remove or move the root trigger; never move a block under its own descendant; for "add", parentId must be an existing block id (or one added earlier in the same edits array); give each edit a short present-tense "note" for narration. If the user is only ASKING about the automation (not changing it), return an empty "edits" array and put the answer in "message".

BLOCK CATALOG:
{catalog}

RESOURCES (the user's existing items — reference these by id):
{resources}
{current_automation}"""


class GenerateAutomationRequest(BaseModel):
    goal: str
    url: Optional[str] = None
    platform: str = "cloud"
    catalog_digest: str = ""
    resource_context: dict = Field(default_factory=dict)
    # When present, the AI EXTENDS this existing automation (returns only new blocks
    # parented onto it) instead of building from scratch. Shape: {name, description, blocks}.
    current_automation: Optional[dict] = None


class GenerateAutomationResponse(BaseModel):
    automation: dict
    message: str = ""
    source: str = "cloud"
    credits_used: int = 0


@router.post(
    "/generate-automation",
    response_model=GenerateAutomationResponse,
    dependencies=[Depends(_rate_limit_ai), Depends(require_feature("ai_workflows"))],
)
async def ai_generate_automation(
    req: GenerateAutomationRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Generate a full automation (block tree) from a natural-language goal."""

    current_section = ""
    if req.current_automation and isinstance(req.current_automation, dict) and req.current_automation.get("blocks"):
        current_section = (
            "\nCURRENT AUTOMATION (extend this, do not rebuild):\n"
            + json.dumps(req.current_automation, ensure_ascii=False)[:12000]
        )
    system = (
        AUTOMATION_SYSTEM.replace("{catalog}", req.catalog_digest or "(none provided)")
        .replace("{resources}", json.dumps(req.resource_context or {}, ensure_ascii=False)[:12000])
        .replace("{current_automation}", current_section)
    )
    user_text = f"GOAL: {req.goal}" + (f"\n\nSTARTING URL: {req.url}" if req.url else "")
    messages = [{"role": "user", "content": [{"type": "text", "text": user_text}]}]

    try:
        ai_text, input_tokens, output_tokens, ai_model = await _call_ai(
            messages=messages,
            max_tokens=await ai_limits.resolve_max_tokens(db, "agent", 2500),
            purpose="assist", system_prompt=system,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_http_error(e, "AI request failed. Please try again.", action="ai_assist.generate_automation")

    spec = _loads_lenient(_strip_code_fence(ai_text))
    if not isinstance(spec, dict) or not isinstance(spec.get("blocks"), list):
        logger.error(f"[AI Assist Automation] invalid spec: {ai_text[:200]}")
        raise HTTPException(status_code=500, detail="AI returned an unusable automation. Please rephrase.")
    spec.setdefault("name", "")
    spec.setdefault("description", "")
    spec.setdefault("block_notes", {})
    spec.setdefault("unresolved", [])
    spec.setdefault("new_resources", [])
    spec.setdefault("rationale", "")
    spec.setdefault("requires_cloud", False)

    cost_micros = _compute_cost_micros(input_tokens, output_tokens, ai_model)
    credits = 0  # single-user coordinator: no credit metering
    ref_id = str(uuid.uuid4())
    await db.commit()

    logger.info(
        f"[AI Assist Automation] credits={credits} "
        f"blocks={len(spec.get('blocks', []))}"
    )
    return GenerateAutomationResponse(
        automation=spec,
        message=spec.get("rationale", "") or "Generated automation",
        source="cloud",
        credits_used=credits,
    )
