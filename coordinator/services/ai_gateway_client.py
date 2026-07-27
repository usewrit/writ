"""
Backend-side AI Gateway client.

For backend services (ai_assist, etc.) that need AI calls.
Uses HTTP (not WS) since the backend and gateway are co-located on the same network.

All AI calls across the entire platform route through here or the recorder's
WS-based GatewayAIClient — both hit the same gateway microservice.
"""
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

import httpx
from utils.http_client import http_session
from utils.http_retry import request_with_retry

logger = logging.getLogger(__name__)

AI_GATEWAY_URL = os.getenv("AI_GATEWAY_URL", "http://localhost:8090")
GATEWAY_SECRET = os.getenv("GATEWAY_SECRET", "")


class AIGatewayError(Exception):
    pass


async def complete(
    messages: List[Dict[str, Any]],
    system: Optional[str] = None,
    max_tokens: int = 500,
    purpose: str = "assist",
    model: Optional[str] = None,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send a completion request to the AI Gateway.

    `user` is a stable conversation id, forwarded to the provider so that
    streaming-backed endpoints route each conversation to its own browser tab.

    Returns: {"content": str, "usage": {"input_tokens": int, "output_tokens": int}, "model": str}
    Raises AIGatewayError on failure.

    BYO routing: when the owner opted into bring-your-own AI keys and one of the
    connected agents advertises keys, the completion runs on THAT agent's local
    keys (nothing managed is touched). Falls back to the managed gateway below per
    the configured policy. `BYOUnavailable` (strict mode, no agent) propagates.
    """
    try:
        from services.byo_ai_router import route as _byo_route
    except Exception:  # router import problem must never break managed AI
        _byo_route = None
    if _byo_route is not None:
        handled, byo_result = await _byo_route(
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            model=model,
            purpose=purpose,
        )
        if handled:
            return byo_result

    return await _managed_complete(
        messages, system=system, max_tokens=max_tokens,
        purpose=purpose, model=model, user=user,
    )


async def _managed_complete(
    messages: List[Dict[str, Any]],
    system: Optional[str] = None,
    max_tokens: int = 500,
    purpose: str = "assist",
    model: Optional[str] = None,
    user: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a completion IN-PROCESS against the owner-configured AI provider.

    Self-host is its own AI gateway: there is no separate ai-gateway microservice,
    so we ALWAYS route to services.local_ai (which raises a clean
    "No AI provider configured. Add one in Settings → AI." error when none is set,
    rather than silently trying to reach an unreachable internal host). The external
    managed-gateway HTTP path below is dead in self-host and reached ONLY if an
    operator explicitly opts in via AI_GATEWAY_EXTERNAL=1 (the hosted deployment)."""
    if os.getenv("AI_GATEWAY_EXTERNAL", "").strip().lower() not in ("1", "true", "yes"):
        from services.local_ai import local_complete
        return await local_complete(
            messages, system=system, max_tokens=max_tokens, model=model, purpose=purpose,
        )

    request_id = str(uuid.uuid4())

    body = {
        "messages": messages,
        "max_tokens": max_tokens,
        "purpose": purpose,
        "request_id": request_id,
    }
    if system:
        body["system"] = system
    if model:
        body["model"] = model
    if user:
        body["user"] = user

    headers = {}
    if GATEWAY_SECRET:
        headers["X-Gateway-Secret"] = GATEWAY_SECRET

    # Retry transient gateway failures (5xx / timeout) with backoff so a brief
    # blip doesn't fail the whole AI operation.
    resp = await request_with_retry(
        "POST",
        f"{AI_GATEWAY_URL}/v1/completions",
        json=body,
        headers=headers,
        timeout=180.0,
    )

    if resp.status_code != 200:
        raise AIGatewayError(f"Gateway returned {resp.status_code}: {resp.text[:200]}")

    return resp.json()


async def complete_text(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1500,
    purpose: str = "assist",
    user: Optional[str] = None,
) -> tuple:
    """
    Text completion → (response_text, input_tokens, output_tokens).
    Drop-in replacement for ai_assist._call_ai().

    `user` is a stable conversation id → routes streaming-backed providers to
    a dedicated tab.
    """
    messages = [{"role": "user", "content": user_prompt}]
    result = await complete(
        messages, system=system_prompt,
        max_tokens=max_tokens, purpose=purpose, user=user,
    )
    return (
        result.get("content", ""),
        result.get("usage", {}).get("input_tokens", 0),
        result.get("usage", {}).get("output_tokens", 0),
    )


async def complete_vision(
    system_prompt: str,
    screenshot_b64: str,
    text_prompt: str,
    max_tokens: int = 1500,
    purpose: str = "assist",
    user: Optional[str] = None,
) -> tuple:
    """
    Vision completion → (response_text, input_tokens, output_tokens).
    For ai_assist chat with screenshots.

    `user` is a stable conversation id → routes streaming-backed providers to
    a dedicated tab.
    """
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64},
            },
            {"type": "text", "text": text_prompt},
        ],
    }]
    result = await complete(
        messages, system=system_prompt,
        max_tokens=max_tokens, purpose=purpose, user=user,
    )
    return (
        result.get("content", ""),
        result.get("usage", {}).get("input_tokens", 0),
        result.get("usage", {}).get("output_tokens", 0),
    )


async def complete_json(
    prompt: str,
    system: Optional[str] = None,
    max_tokens: int = 2000,
    purpose: str = "workflow",
    user: Optional[str] = None,
) -> Optional[Dict]:
    """
    Text completion → parsed JSON dict.

    `user` is a stable conversation id → routes streaming-backed providers to
    a dedicated tab.
    """
    messages = [{"role": "user", "content": prompt}]
    result = await complete(
        messages, system=system,
        max_tokens=max_tokens, purpose=purpose, user=user,
    )
    text = result.get("content", "").strip()
    return _parse_json(text), result.get("usage", {})


def _parse_json(text: str) -> Optional[Dict]:
    """Parse JSON from AI response, handling markdown code blocks."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    logger.warning(f"Could not parse JSON from AI response: {text[:200]}")
    return None
