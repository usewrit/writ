"""OpenAI-compatible chat surface over a live streaming browser session.

A running streaming session (see :mod:`services.streaming_manager`) drives a real
chat model inside a browser tab through a named handler (``ps.page.*``). This
module exposes that session as an **OpenAI-compatible API** — Chat Completions,
the Responses API and a ``models`` list — so any OpenAI SDK / client can talk to a
browser-backed model as if it were a normal provider endpoint.

The translation, in both directions:

  request  (OpenAI)                         handler payload (browser)
  ─────────────────────────────────────     ─────────────────────────────────────
  messages + tools + functions          →   {message, images, files, messages,
                                             _thread_id}
  multimodal image/file/audio parts     →   images[] (vision) + files[] (upload
                                             descriptors, via resolve_turn_attachments)
  OpenAI tool/function definitions       →   text tool instructions in `message`

  handler result (browser)                  response (OpenAI)
  ─────────────────────────────────────     ─────────────────────────────────────
  text                                   →   chat.completion / response text
  fenced ```tool_call blocks             →   OpenAI tool_calls (finish=tool_calls)
  multimodal blocks (text + image_url)   →   Responses output (message + image
                                             generation items)

Relay
-----
There is no ws-gateway and no Redis command relay here. Commands go straight to
the owning fleet agent through the in-process :data:`services.streaming_manager
.manager`:

  * ``manager.invoke_streaming(session_key, handler, data, timeout)`` yields
    ``{"type": "chunk"|"keepalive"|"done", "data": ...}`` frames (streaming).
  * ``manager.invoke(session_key, handler, data, timeout)`` returns the terminal
    payload dict (non-streaming).

Thread routing (multi-conversation)
-----------------------------------
When a workflow enables ``multi_conversation``, distinct conversations are routed
to distinct browser tabs (threads), capped at ``max_concurrent_threads``. The
router is content-fingerprint based (last user→assistant exchange, then last
assistant, then full history) so unrelated conversations don't collide on a shared
opening. The chosen thread is passed to the handler as ``_thread_id``.

The fingerprint→thread signal cache is kept in the coordinator's in-process Redis
(``utils.redis_client.get_redis`` — a fakeredis client backing one shared
keyspace, NOT an external server), reusing the exact ``thread:sig:*`` / ``thread:
pool:*`` / ``thread:seq:*`` keys. If that client is unavailable the router
degrades to "always the main tab" (returns ``None``) — thread stickiness is an
optimization, never a correctness requirement.

Notes
-----
This surface is single-owner and in-process:

  * ``resolve_turn_attachments`` is called with the request ``AsyncSession``
    (files resolve directly through ``file_service``).
  * No billing / metering / credit-ledger calls. Usage token counts are still
    computed and returned in the OpenAI ``usage`` block (clients rely on the field
    being present and real), but nothing is charged.

Everything here is framework-agnostic: functions take plain args + an
``AsyncSession`` and return dicts or async generators of SSE ``data: {...}\\n\\n``
strings. The router wraps them in FastAPI responses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import unicodedata
import uuid
from typing import Any, AsyncIterator, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from services.streaming_manager import manager
from services.streaming_tool_bridge import (
    build_message_with_tools,
    build_tool_call_response,
    extract_attachments,
    parse_tool_calls,
)
from services.streaming_attachments import resolve_turn_attachments

logger = logging.getLogger(__name__)

# Per-turn handler timeout. Browser chat turns (and especially image generation)
# can be slow, so this is deliberately generous — matches the cloud original.
_HANDLER_TIMEOUT = 660

# TTL for a fingerprint→thread signal in the routing cache (seconds).
_THREAD_SIGNAL_TTL = 86400


# ── streaming_config knobs ──────────────────────────────────────────────────

def _resolve_stream_config(streaming_config: Optional[dict]) -> dict:
    """Derive the per-turn OpenAI-surface knobs from a workflow's streaming_config.

    ``default_handler`` is resolved here for completeness, but callers of
    ``chat_completions`` / ``responses`` pass the handler name explicitly (the
    router owns handler resolution), so it is only used as a fallback.
    """
    sc = streaming_config or {}
    oc = sc.get("openai_compat", {}) or {}
    mc = oc.get("model", {}) or {}
    return {
        "default_handler": oc.get("default_handler", "chat"),
        "model_name": mc.get("name") or oc.get("model_name", "streaming"),
        "response_field": oc.get("response_field", "content"),
        "multi_conversation": bool(sc.get("multi_conversation", False)),
        "max_concurrent_threads": int(sc.get("max_concurrent_threads", 3) or 3),
    }


async def _load_streaming_config(db: AsyncSession, session_key: str) -> dict:
    """Load the resolved streaming config for a session's workflow.

    The single-container coordinator has cheap in-process DB access and the hot
    path is bounded by a whole browser round-trip, so (unlike the cloud, which
    cached this per session to avoid a network db.get) we just read it directly —
    always fresh, no cache to invalidate on config edits.
    """
    from models.streaming_session import StreamingSession
    from models.automation_workflow import AutomationWorkflow
    from sqlalchemy import select

    workflow_id = await db.scalar(
        select(StreamingSession.workflow_id).where(
            StreamingSession.session_key == session_key
        )
    )
    wf = await db.get(AutomationWorkflow, workflow_id) if workflow_id else None
    return _resolve_stream_config(getattr(wf, "streaming_config", None) if wf else None)


# ── Content shaping helpers (ported inline — pure logic, no cloud coupling) ──

def _extract_text_from_content(content: Any) -> str:
    """Extract plain text from content that may be a string or multimodal blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (
                "text", "input_text", "output_text",
            ):
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content) if content else ""


def _extract_images_from_content(content: Any) -> List[dict]:
    """Extract image data URLs + revised prompts from multimodal content blocks.

    Returns ``[{"url": "data:...", "revised_prompt": "..."}, ...]``.
    """
    if not isinstance(content, list):
        return []
    images = []
    for block in content:
        if isinstance(block, dict) and block.get("type") in ("image_url", "input_image"):
            # Chat Completions nests {url}; Responses API gives image_url as a string.
            iu = block.get("image_url", "")
            url = iu.get("url", "") if isinstance(iu, dict) else (iu if isinstance(iu, str) else "")
            if url:
                images.append({
                    "url": url,
                    "revised_prompt": block.get("revised_prompt", "") or "Generating image...",
                })
    return images


def _normalize_responses_input(inp: Any) -> List[dict]:
    """Normalize a Responses API ``input`` (string or messages list) into messages."""
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    if isinstance(inp, list):
        return inp
    return [{"role": "user", "content": str(inp)}]


def _strip_inline_base64(messages: List[dict]) -> List[dict]:
    """Replace large inline base64 blocks with lightweight placeholders.

    Attachments are already extracted separately (``extract_attachments`` /
    ``resolve_turn_attachments``), so the raw base64 in ``messages`` is dead weight
    that also risks JSON-serialization issues (control chars) in the handler scope.
    Messages without a base64 block are passed through unchanged (no copy).
    """
    import copy

    base64_types = ("image_url", "input_image", "input_audio", "file", "document")
    cleaned = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            cleaned.append(msg)
            continue
        if not any(
            isinstance(part, dict) and part.get("type") in base64_types
            for part in content
        ):
            cleaned.append(msg)
            continue
        new_content = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in base64_types:
                new_content.append({"type": part.get("type"), "_stripped": True})
            else:
                new_content.append(part)
        new_msg = copy.copy(msg)
        new_msg["content"] = new_content
        cleaned.append(new_msg)
    return cleaned


# ── Usage accounting (token counts only — nothing is billed) ────────────────

_TIKTOKEN_ENC = None
_TIKTOKEN_TRIED = False


def _get_encoder():
    """Lazily load a tiktoken encoder; returns None if tiktoken isn't installed."""
    global _TIKTOKEN_ENC, _TIKTOKEN_TRIED
    if _TIKTOKEN_TRIED:
        return _TIKTOKEN_ENC
    _TIKTOKEN_TRIED = True
    try:
        import tiktoken
        _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _TIKTOKEN_ENC = None
    return _TIKTOKEN_ENC


def _count_tokens(text: str) -> int:
    """Count tokens — exact via tiktoken when available, else ~4 chars/token.

    The browser-backed handler doesn't itself return token counts, so we count the
    real input/output text to populate a truthful OpenAI ``usage`` block.
    """
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    return max(1, (len(text) + 3) // 4)


def _coerce_usage_from_result(result: Any, want: str) -> Optional[dict]:
    """Normalize a handler-supplied ``usage`` block (e.g. forwarded from an AI
    gateway) to the requested OpenAI shape. ``want`` is "chat" or "responses".
    Returns None when the result carries no usable usage.
    """
    if not isinstance(result, dict):
        return None
    raw = result.get("usage")
    if not isinstance(raw, dict):
        return None
    inp = raw.get("prompt_tokens", raw.get("input_tokens"))
    out = raw.get("completion_tokens", raw.get("output_tokens"))
    if inp is None and out is None:
        return None
    inp = int(inp or 0)
    out = int(out or 0)
    total = int(raw.get("total_tokens") or (inp + out))
    if want == "responses":
        return {
            "input_tokens": inp,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": out,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": total,
        }
    return {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": total}


def _chat_usage_from_text(input_text: str, output_text: str) -> dict:
    pt = _count_tokens(input_text)
    ct = _count_tokens(output_text)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}


def _responses_usage_from_text(input_text: str, output_text: str) -> dict:
    it = _count_tokens(input_text)
    ot = _count_tokens(output_text)
    return {
        "input_tokens": it,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": ot,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": it + ot,
    }


def _resolve_chat_usage(result: Any, input_text: str, output_text: str) -> dict:
    """Prefer real gateway usage from the handler result; else count text."""
    return _coerce_usage_from_result(result, "chat") or _chat_usage_from_text(input_text, output_text)


def _resolve_responses_usage(result: Any, input_text: str, output_text: str) -> dict:
    return _coerce_usage_from_result(result, "responses") or _responses_usage_from_text(input_text, output_text)


# ── Responses API object shaping ────────────────────────────────────────────

def _build_response_object(
    response_id: str,
    model_name: str,
    created: int,
    status: str,
    output: list,
    *,
    has_images: bool = False,
    instructions: Optional[str] = None,
    temperature: Optional[float] = None,
    usage: Optional[dict] = None,
) -> dict:
    """Build a complete OpenAI Responses API ``response`` object."""
    tools = [{"type": "image_generation"}] if has_images else []
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "completed_at": created if status == "completed" else None,
        "error": None,
        "incomplete_details": None,
        "instructions": instructions,
        "max_output_tokens": None,
        "model": model_name,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": temperature if temperature is not None else 1.0,
        "text": {"format": {"type": "text"}},
        "tool_choice": "auto",
        "tools": tools,
        "top_p": 1.0,
        "truncation": "disabled",
        "usage": usage or {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        },
        "user": None,
        "metadata": {},
    }


def _format_responses_output(content: Any, response_id: str) -> list:
    """Format handler content (string or multimodal list) into the Responses output
    array — a message item with text plus any ``image_generation_call`` items."""
    text = _extract_text_from_content(content)
    images = _extract_images_from_content(content)

    output = []
    if text:
        output.append({
            "type": "message",
            "id": f"msg_{response_id[:16]}",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for i, img_info in enumerate(images):
        img_url = img_info["url"]
        b64 = img_url.split(",", 1)[1] if "," in img_url else img_url
        output.append({
            "type": "image_generation_call",
            "id": f"ig_{response_id[:12]}_{i}",
            "status": "completed",
            "result": b64,
            "revised_prompt": img_info.get("revised_prompt", "Generating image..."),
            "background": "opaque",
            "output_format": "png",
            "quality": "high",
            "size": "1024x1024",
        })
    return output


# ── Thread detection (multi-conversation tab routing) ───────────────────────

def _normalize_for_fp(text: str) -> str:
    """Normalize text for consistent fingerprinting (unicode, whitespace)."""
    text = unicodedata.normalize("NFC", text)
    text = " ".join(text.strip().split())
    return text[:200]


def _text_messages(messages: List[dict]) -> List[Tuple[str, str]]:
    """Return ``[(role, normalized_text), ...]`` for messages carrying real text."""
    out = []
    for msg in messages or []:
        role = msg.get("role", "")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text_from_content(msg.get("content", ""))
        if text and text.strip():
            out.append((role, _normalize_for_fp(text)))
    return out


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _conversation_signals(messages: List[dict]) -> List[str]:
    """Discriminating signals identifying WHICH conversation this is, strongest first.

    Keyed on the state both sides have agreed on (everything up to and including the
    last assistant message), so generic openings don't collide different chats:

      1. recent   — last (user → assistant) exchange (near-unique, our text).
      2. lastasst — just the last assistant message (survives an edited user turn).
      3. full     — hash of the entire settled history (exact continuation match).

    Returns [] when there is no assistant turn yet (a brand-new conversation).
    """
    texts = _text_messages(messages)
    last_asst_i = None
    for i in range(len(texts) - 1, -1, -1):
        if texts[i][0] == "assistant":
            last_asst_i = i
            break
    if last_asst_i is None:
        return []

    last_asst = texts[last_asst_i][1]
    prev_user = ""
    for j in range(last_asst_i - 1, -1, -1):
        if texts[j][0] == "user":
            prev_user = texts[j][1]
            break

    full = "\n".join(f"{r}:{t}" for r, t in texts[: last_asst_i + 1])
    return [
        _h("recent|" + prev_user + "|" + last_asst),
        _h("lastasst|" + last_asst),
        _h("full|" + full),
    ]


def _routing_redis():
    """Return the in-process routing cache client, or None if unavailable.

    The coordinator's ``get_redis`` is a synchronous getter returning an async
    fakeredis client (one shared in-process keyspace — no external server). Thread
    stickiness is a best-effort optimization, so any failure to obtain the client
    degrades to single-tab routing rather than erroring.
    """
    try:
        from utils.redis_client import get_redis
        return get_redis()
    except Exception:
        logger.debug("streaming_openai: routing cache unavailable — single-tab routing")
        return None


async def _detect_thread_id(
    session_key: str,
    messages: List[dict],
    explicit_user: Optional[str],
    *,
    multi_conv: bool,
    max_threads: int,
) -> Optional[str]:
    """Route a conversation to a browser tab (thread) when multi-conversation is on.

    Returns None to use the default (main) tab. See the module docstring. When
    ``multi_conv`` is False this always returns None (single tab).
    """
    if not multi_conv:
        return None
    if explicit_user:
        return explicit_user

    redis = _routing_redis()
    if redis is None:
        return None

    pool_key = f"thread:pool:{session_key}"

    # Match on discriminating signals (recent exchange → last assistant → full
    # history). First hit wins (strongest first).
    signals = _conversation_signals(messages)
    if signals:
        sig_keys = [f"thread:sig:{session_key}:{sig}" for sig in signals]
        try:
            cached_vals = await redis.mget(sig_keys)
        except Exception:
            cached_vals = [None] * len(sig_keys)
        for sig, cached in zip(signals, cached_vals):
            if cached:
                logger.debug("Thread detect: signal hit (%s) → %s", sig[:8], cached)
                return None if cached == "default" else cached

    try:
        members = await redis.smembers(pool_key)
    except Exception:
        members = set()

    # Continued conversation but no signal matched (history trimmed / turn missed):
    # a single existing thread almost certainly owns it — route there.
    if signals and len(members) == 1:
        only = next(iter(members))
        thread_id = None if only == "default" else only
        logger.info("Thread detect: fallback to single thread → %s", only)
        try:
            pipe = redis.pipeline()
            for sig in signals:
                pipe.set(f"thread:sig:{session_key}:{sig}", only, ex=_THREAD_SIGNAL_TTL)
            await pipe.execute()
        except Exception:
            pass
        return thread_id

    # New conversation → assign a thread (cap-aware).
    active_count = len(members)
    if active_count == 0:
        thread_id = None
        effective = "default"
    elif active_count < max(1, max_threads):
        try:
            seq = await redis.incr(f"thread:seq:{session_key}")
        except Exception:
            seq = active_count + 1
        thread_id = f"auto-{seq}"
        effective = thread_id
    else:
        # At capacity — reuse an existing thread round-robin instead of growing.
        ordered = sorted(members)
        try:
            idx = await redis.incr(f"thread:rr:{session_key}")
        except Exception:
            idx = 0
        effective = ordered[idx % len(ordered)]
        thread_id = None if effective == "default" else effective
        logger.info("Thread detect: at capacity (%d/%d) → reuse %s", active_count, max_threads, effective)

    logger.info("Thread detect: new conversation → %s (pool size=%d)", effective, active_count)

    # Register this conversation's signals → thread so the next turn sticks here.
    try:
        pipe = redis.pipeline()
        for sig in signals:
            pipe.set(f"thread:sig:{session_key}:{sig}", effective, ex=_THREAD_SIGNAL_TTL)
        pipe.sadd(pool_key, effective)
        pipe.expire(pool_key, _THREAD_SIGNAL_TTL)
        await pipe.execute()
    except Exception:
        pass

    return thread_id


async def _cache_thread_for_next_turn(
    session_key: str,
    messages: List[dict],
    thread_id: Optional[str],
    response_content: Any,
) -> None:
    """Pre-cache the signals the NEXT turn will present (this turn + our response)
    so it routes back to the same tab. Computed via the same ``_conversation_
    signals`` used for detection, on ``messages + [assistant response]``.
    """
    text_content = _extract_text_from_content(response_content)
    if not text_content or len(text_content.strip()) < 3:
        return
    redis = _routing_redis()
    if redis is None:
        return

    effective = thread_id or "default"
    next_messages = list(messages) + [{"role": "assistant", "content": text_content}]
    signals = _conversation_signals(next_messages)
    if not signals:
        return
    try:
        pipe = redis.pipeline()
        for sig in signals:
            pipe.set(f"thread:sig:{session_key}:{sig}", effective, ex=_THREAD_SIGNAL_TTL)
        await pipe.execute()
        logger.debug("Thread cache: %s ← %d signals", effective, len(signals))
    except Exception:
        pass


# ── Shared turn assembly ────────────────────────────────────────────────────

async def _assemble_turn(
    db: AsyncSession,
    session_key: str,
    messages: List[dict],
    cfg: dict,
    *,
    tools: Optional[List[dict]],
    functions: Optional[List[dict]],
    explicit_user: Optional[str],
    thread_routing: bool,
    strip_base64_in_history: bool,
) -> Tuple[str, List[str], List[dict], Optional[str], List[dict]]:
    """Build the handler payload for one turn.

    Returns ``(message_to_send, images, files, thread_id, handler_messages)``:
      - ``message_to_send``   : the single text prompt (tools + results folded in).
      - ``images``            : vision data/URL strings.
      - ``files``             : per-session upload descriptors for set_input_files.
      - ``thread_id``         : chosen tab (None = main), or None if routing is off.
      - ``handler_messages``  : the ``messages`` list to hand the handler (base64
                                stripped for the Responses path).
    """
    message_to_send = build_message_with_tools(messages, tools=tools, functions=functions)

    # Attachments come from ONLY the last user message (not the whole history).
    last_user_messages: List[dict] = []
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user_messages = [msg]
            break
    attachments = extract_attachments(last_user_messages)
    images = [a["url"] for a in attachments if a["type"] == "image"]
    files = [a for a in attachments if a["type"] in ("file", "audio")]

    # Resolve inline attachments + file_id references into vision images and
    # per-session upload descriptors. Single-owner: no tenant_id, just the session.
    resolved_images, session_files = await resolve_turn_attachments(db, messages, attachments)
    if resolved_images:
        images = images + resolved_images
    if session_files:
        files = files + session_files

    thread_id = None
    if thread_routing:
        thread_id = await _detect_thread_id(
            session_key, messages, explicit_user,
            multi_conv=cfg["multi_conversation"],
            max_threads=cfg["max_concurrent_threads"],
        )

    handler_messages = _strip_inline_base64(messages) if strip_base64_in_history else messages
    return message_to_send, images, files, thread_id, handler_messages


def _final_content_from_done(done_data: Any, response_field: str) -> Any:
    """Pull the effective content out of a handler's terminal ``done`` payload."""
    if isinstance(done_data, dict):
        content = done_data.get("content", "") or done_data.get(response_field, "")
        if not content and done_data.get("success") is not None:
            content = done_data.get("content", "") or json.dumps(done_data)
        return content
    if isinstance(done_data, str):
        return done_data
    return ""


# ── Public API: models ──────────────────────────────────────────────────────

def list_models(workflow_id: int, streaming_config: dict) -> dict:
    """OpenAI ``GET /v1/models`` shape — the session's single model.

    Pure function of the workflow id (used only for a stable ``created`` seed) and
    its ``streaming_config`` (which carries the advertised model metadata).
    """
    sc = streaming_config or {}
    oc = sc.get("openai_compat", {}) or {}
    mc = oc.get("model", {}) or {}
    model_name = mc.get("name") or oc.get("model_name", "streaming")
    caps = mc.get("capabilities", {}) or {}
    limits = mc.get("limits", {}) or {}
    return {
        "object": "list",
        "data": [{
            "id": model_name,
            "object": "model",
            "created": int(workflow_id) if workflow_id else 0,
            "owned_by": "writ",
            "description": mc.get("description", ""),
            "version": mc.get("version", "1.0"),
            "capabilities": {
                "vision": caps.get("vision", False),
                "function_calling": caps.get("tools", True),
                "streaming": caps.get("streaming", True),
                "file_upload": caps.get("file_upload", False),
            },
            "limits": {
                "context_window": limits.get("context_window", 128000),
                "max_output_tokens": limits.get("max_output_tokens", 8192),
            },
        }],
    }


# ── Public API: chat completions ────────────────────────────────────────────

async def chat_completions(
    db: AsyncSession,
    session_key: str,
    req: dict,
    *,
    handler_name: str,
    thread_routing: bool,
) -> dict:
    """Non-streaming OpenAI Chat Completions. Returns a ``chat.completion`` dict.

    ``req`` is the raw OpenAI request body (``messages``, ``tools``, ``functions``,
    ``user`` …). ``handler_name`` is the browser handler to invoke; the router
    resolves it. ``thread_routing`` gates multi-conversation tab routing.
    """
    messages = req.get("messages") or []
    tools = req.get("tools")
    functions = req.get("functions")

    cfg = await _load_streaming_config(db, session_key)
    model_name = cfg["model_name"]
    response_field = cfg["response_field"]

    message_to_send, images, files, thread_id, handler_messages = await _assemble_turn(
        db, session_key, messages, cfg,
        tools=tools, functions=functions, explicit_user=req.get("user"),
        thread_routing=thread_routing, strip_base64_in_history=False,
    )

    effective_tools = tools or []
    if not effective_tools and functions:
        effective_tools = [{"type": "function", "function": f} for f in functions]

    completion_id = f"chatcmpl-{session_key[:8]}-{uuid.uuid4().hex[:8]}"
    created = int(time.time())
    sys_fp = f"thread:{thread_id or 'default'}"

    result = await manager.invoke(
        session_key, handler_name,
        {
            "message": message_to_send, "images": images, "files": files,
            "messages": handler_messages, "_thread_id": thread_id,
        },
        timeout=_HANDLER_TIMEOUT,
    )
    if result is None:
        raise TimeoutError("Handler timeout — no response from agent")

    if isinstance(result, str):
        content: Any = result
    elif isinstance(result, dict):
        content = result.get(response_field, result.get("content", json.dumps(result)))
    else:
        content = str(result)

    await _cache_thread_for_next_turn(session_key, messages, thread_id, content)

    text_content = _extract_text_from_content(content)
    usage = _resolve_chat_usage(result, message_to_send, text_content)

    if effective_tools and text_content:
        tool_calls, remaining = parse_tool_calls(text_content, effective_tools)
        if tool_calls:
            return build_tool_call_response(
                completion_id, model_name, tool_calls, remaining, created, usage=usage,
            )

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "system_fingerprint": sys_fp,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text_content},
            "finish_reason": "stop",
        }],
        "usage": usage,
    }


async def chat_completions_stream(
    db: AsyncSession,
    session_key: str,
    req: dict,
    *,
    handler_name: str,
    thread_routing: bool,
) -> AsyncIterator[str]:
    """Streaming OpenAI Chat Completions. Yields SSE ``data: {...}\\n\\n`` strings,
    ending with the terminal ``data: [DONE]\\n\\n``."""
    messages = req.get("messages") or []
    tools = req.get("tools")
    functions = req.get("functions")

    cfg = await _load_streaming_config(db, session_key)
    model_name = cfg["model_name"]
    response_field = cfg["response_field"]

    message_to_send, images, files, thread_id, handler_messages = await _assemble_turn(
        db, session_key, messages, cfg,
        tools=tools, functions=functions, explicit_user=req.get("user"),
        thread_routing=thread_routing, strip_base64_in_history=False,
    )

    effective_tools = tools or []
    if not effective_tools and functions:
        effective_tools = [{"type": "function", "function": f} for f in functions]

    completion_id = f"chatcmpl-{session_key[:8]}-{uuid.uuid4().hex[:8]}"
    created = int(time.time())

    def _chunk(choices: list, usage: Optional[dict] = None) -> str:
        env = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": choices,
        }
        if usage is not None:
            env["usage"] = usage
        return f"data: {json.dumps(env)}\n\n"

    # Reuse one envelope for per-delta content ticks; mutate only the content.
    _delta_envelope = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_name,
        "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}],
    }
    _delta_content = _delta_envelope["choices"][0]["delta"]

    async for chunk in manager.invoke_streaming(
        session_key, handler_name,
        {
            "message": message_to_send, "images": images, "files": files,
            "messages": handler_messages, "_thread_id": thread_id,
        },
        timeout=_HANDLER_TIMEOUT,
    ):
        ctype = chunk.get("type")
        if ctype == "keepalive":
            yield ": keepalive\n\n"
            continue

        if ctype == "chunk":
            data = chunk.get("data", {})
            content = data if isinstance(data, str) else data.get("content", "")
            _delta_content["content"] = content
            yield f"data: {json.dumps(_delta_envelope)}\n\n"
            continue

        if ctype == "done":
            done_data = chunk.get("data", {})
            final_content = _final_content_from_done(done_data, response_field)
            final_text = _extract_text_from_content(final_content)

            tool_calls = None
            if final_content:
                await _cache_thread_for_next_turn(session_key, messages, thread_id, final_content)
                tool_calls, remaining = (
                    parse_tool_calls(final_text, effective_tools) if effective_tools else (None, None)
                )
                if tool_calls:
                    if remaining:
                        yield _chunk([{
                            "index": 0, "delta": {"content": remaining}, "finish_reason": None,
                        }])
                    yield _chunk([{
                        "index": 0, "delta": {"tool_calls": tool_calls}, "finish_reason": "tool_calls",
                    }])
                else:
                    # Chat Completions: text-only per OpenAI spec.
                    text_to_send = final_text if isinstance(final_content, list) else final_content
                    if text_to_send:
                        yield _chunk([{
                            "index": 0, "delta": {"content": text_to_send}, "finish_reason": None,
                        }])

            has_tool_calls = tool_calls if (effective_tools and final_text) else None
            yield _chunk([{
                "index": 0, "delta": {},
                "finish_reason": "tool_calls" if has_tool_calls else "stop",
            }])

            usage = _resolve_chat_usage(done_data, message_to_send, final_text)
            yield _chunk([], usage=usage)
            yield "data: [DONE]\n\n"
            return

    yield "data: [DONE]\n\n"


# ── Public API: Responses API ───────────────────────────────────────────────

async def responses(
    db: AsyncSession,
    session_key: str,
    req: dict,
    *,
    handler_name: str,
) -> dict:
    """Non-streaming OpenAI Responses API. Returns a ``response`` object (text +
    any image-generation output). Thread routing follows the workflow's
    ``multi_conversation`` toggle (always applied for the Responses surface)."""
    cfg = await _load_streaming_config(db, session_key)
    model_name = cfg["model_name"]
    response_field = cfg["response_field"]

    messages = _normalize_responses_input(req.get("input"))
    instructions = req.get("instructions")
    if instructions:
        # The canonical Responses system prompt is a sibling of `input`; fold it in
        # as a leading system message so it surfaces as [System: ...] context.
        messages = [{"role": "system", "content": instructions}] + messages

    message_to_send, images, files, thread_id, handler_messages = await _assemble_turn(
        db, session_key, messages, cfg,
        tools=req.get("tools"), functions=None, explicit_user=req.get("user"),
        thread_routing=True, strip_base64_in_history=True,
    )

    response_id = f"resp_{session_key[:8]}_{uuid.uuid4().hex[:8]}"
    created = int(time.time())

    result = await manager.invoke(
        session_key, handler_name,
        {
            "message": message_to_send, "images": images, "files": files,
            "messages": handler_messages, "_thread_id": thread_id,
        },
        timeout=_HANDLER_TIMEOUT,
    )
    if result is None:
        raise TimeoutError("Handler timeout — no response from agent")

    if isinstance(result, str):
        content: Any = result
    elif isinstance(result, dict):
        content = result.get(response_field, result.get("content", json.dumps(result)))
    else:
        content = str(result)

    await _cache_thread_for_next_turn(session_key, messages, thread_id, content)

    output = _format_responses_output(content, response_id)
    has_images = any(item.get("type") == "image_generation_call" for item in output)
    return _build_response_object(
        response_id, model_name, created, "completed", output,
        has_images=has_images,
        instructions=instructions,
        temperature=req.get("temperature"),
        usage=_resolve_responses_usage(result, message_to_send, _extract_text_from_content(content)),
    )


async def responses_stream(
    db: AsyncSession,
    session_key: str,
    req: dict,
    *,
    handler_name: str,
) -> AsyncIterator[str]:
    """Streaming OpenAI Responses API. Yields the full ``event: <name>\\ndata:
    {...}\\n\\n`` protocol (response.created → output_item/content_part/text
    deltas → image-generation items → response.completed)."""
    cfg = await _load_streaming_config(db, session_key)
    model_name = cfg["model_name"]
    response_field = cfg["response_field"]

    messages = _normalize_responses_input(req.get("input"))
    instructions = req.get("instructions")
    if instructions:
        messages = [{"role": "system", "content": instructions}] + messages

    message_to_send, images, files, thread_id, handler_messages = await _assemble_turn(
        db, session_key, messages, cfg,
        tools=req.get("tools"), functions=None, explicit_user=req.get("user"),
        thread_routing=True, strip_base64_in_history=True,
    )

    response_id = f"resp_{session_key[:8]}_{uuid.uuid4().hex[:8]}"
    created = int(time.time())
    temperature = req.get("temperature")

    seq = 0
    msg_id = f"msg_{response_id[:16]}"
    accumulated_text = ""

    response_skeleton = _build_response_object(
        response_id, model_name, created, "in_progress", [],
        has_images=True,  # declare image_generation upfront
        instructions=instructions, temperature=temperature,
    )
    msg_item = {"type": "message", "id": msg_id, "status": "in_progress", "role": "assistant", "content": []}
    text_part = {"type": "output_text", "text": "", "annotations": []}

    seq += 1
    yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': response_skeleton, 'sequence_number': seq})}\n\n"
    seq += 1
    yield f"event: response.output_item.added\ndata: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': msg_item, 'sequence_number': seq})}\n\n"
    seq += 1
    yield f"event: response.content_part.added\ndata: {json.dumps({'type': 'response.content_part.added', 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': text_part, 'sequence_number': seq})}\n\n"

    _delta_event = {
        "type": "response.output_text.delta",
        "item_id": msg_id, "output_index": 0, "content_index": 0,
        "delta": "", "sequence_number": 0,
    }
    _DELTA_PREFIX = "event: response.output_text.delta\ndata: "

    async for chunk in manager.invoke_streaming(
        session_key, handler_name,
        {
            "message": message_to_send, "images": images, "files": files,
            "messages": handler_messages, "_thread_id": thread_id,
        },
        timeout=_HANDLER_TIMEOUT,
    ):
        ctype = chunk.get("type")
        if ctype == "keepalive":
            yield ": keepalive\n\n"
            continue

        if ctype == "chunk":
            data = chunk.get("data", {})
            text_chunk = data.get("content", "") if isinstance(data, dict) else str(data)
            if text_chunk:
                accumulated_text += text_chunk
                seq += 1
                _delta_event["delta"] = text_chunk
                _delta_event["sequence_number"] = seq
                yield f"{_DELTA_PREFIX}{json.dumps(_delta_event)}\n\n"
            continue

        if ctype == "done":
            done_data = chunk.get("data", {})
            final_content = _final_content_from_done(done_data, response_field)
            final_text = _extract_text_from_content(final_content)
            imgs = _extract_images_from_content(final_content)

            # Final text that wasn't streamed → emit as one delta.
            if final_text and not accumulated_text:
                seq += 1
                yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': final_text, 'sequence_number': seq})}\n\n"

            effective_text = final_text or accumulated_text
            done_text_part = {"type": "output_text", "text": effective_text, "annotations": []}

            seq += 1
            yield f"event: response.output_text.done\ndata: {json.dumps({'type': 'response.output_text.done', 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': effective_text, 'sequence_number': seq})}\n\n"
            seq += 1
            yield f"event: response.content_part.done\ndata: {json.dumps({'type': 'response.content_part.done', 'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': done_text_part, 'sequence_number': seq})}\n\n"

            done_msg_item = {
                "type": "message", "id": msg_id, "status": "completed",
                "role": "assistant", "content": [done_text_part],
            }
            seq += 1
            yield f"event: response.output_item.done\ndata: {json.dumps({'type': 'response.output_item.done', 'output_index': 0, 'item': done_msg_item, 'sequence_number': seq})}\n\n"

            # Image-generation items — full Responses streaming protocol per image.
            for i, img_info in enumerate(imgs):
                img_url = img_info["url"]
                revised_prompt = img_info.get("revised_prompt", "Generating image...")
                b64 = img_url.split(",", 1)[1] if "," in img_url else img_url
                ig_id = f"ig_{response_id[:12]}_{i}"
                out_idx = 1 + i  # message is index 0
                ig_item_pending = {
                    "type": "image_generation_call", "id": ig_id, "status": "in_progress",
                    "revised_prompt": revised_prompt, "result": "",
                    "background": "opaque", "output_format": "png", "quality": "high", "size": "1024x1024",
                }
                ig_item_done = {
                    "type": "image_generation_call", "id": ig_id, "status": "completed",
                    "revised_prompt": revised_prompt, "result": b64,
                    "background": "opaque", "output_format": "png", "quality": "high", "size": "1024x1024",
                }
                seq += 1
                yield f"event: response.output_item.added\ndata: {json.dumps({'type': 'response.output_item.added', 'output_index': out_idx, 'item': ig_item_pending, 'sequence_number': seq})}\n\n"
                seq += 1
                yield f"event: response.image_generation_call.in_progress\ndata: {json.dumps({'type': 'response.image_generation_call.in_progress', 'output_index': out_idx, 'item_id': ig_id, 'sequence_number': seq})}\n\n"
                seq += 1
                yield f"event: response.image_generation_call.generating\ndata: {json.dumps({'type': 'response.image_generation_call.generating', 'output_index': out_idx, 'item_id': ig_id, 'sequence_number': seq})}\n\n"
                seq += 1
                yield f"event: response.image_generation_call.partial_image\ndata: {json.dumps({'type': 'response.image_generation_call.partial_image', 'item_id': ig_id, 'output_index': out_idx, 'partial_image_index': 0, 'partial_image_b64': b64, 'sequence_number': seq})}\n\n"
                seq += 1
                yield f"event: response.image_generation_call.completed\ndata: {json.dumps({'type': 'response.image_generation_call.completed', 'output_index': out_idx, 'item_id': ig_id, 'sequence_number': seq})}\n\n"
                seq += 1
                yield f"event: response.output_item.done\ndata: {json.dumps({'type': 'response.output_item.done', 'output_index': out_idx, 'item': ig_item_done, 'sequence_number': seq})}\n\n"

            await _cache_thread_for_next_turn(session_key, messages, thread_id, final_content)

            output = _format_responses_output(final_content, response_id)
            completed_response = _build_response_object(
                response_id, model_name, created, "completed", output,
                has_images=len(imgs) > 0,
                instructions=instructions, temperature=temperature,
                usage=_resolve_responses_usage(done_data, message_to_send, _extract_text_from_content(final_content)),
            )
            seq += 1
            yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': completed_response, 'sequence_number': seq})}\n\n"
            return


__all__ = [
    "chat_completions",
    "chat_completions_stream",
    "responses",
    "responses_stream",
    "list_models",
]
