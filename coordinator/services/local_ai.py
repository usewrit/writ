"""In-process AI completion — the self-host coordinator IS its own AI gateway.

The hosted product routes AI-assist through a separate ``ai-gateway`` microservice
(``AI_GATEWAY_URL``, :8090). That service is NOT part of the single-container
self-host, so ``ai_gateway_client._managed_complete`` falls back to this module,
which calls the owner-configured provider DIRECTLY over HTTP — exactly like the
"Test" button in Settings → AI (which is why testing works but assist used to say
"AI service unavailable"). Providers: Anthropic, OpenAI, OpenRouter,
OpenAI-Responses, and local OpenAI-compatible servers (Ollama etc.).

Returns the same shape as the gateway: ``{"content", "usage": {"input_tokens",
"output_tokens"}, "model"}``.
"""
import logging
from typing import Any, Dict, List, Optional

from database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# Fallback model per provider when the config row leaves `model` blank. Matches
# the defaults used by the Settings "Test" path so behaviour is consistent.
_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-4-20250514",
    "openai": "gpt-4o-mini",
    "openai_responses": "gpt-4o-mini",
    "openrouter": "openai/gpt-4o-mini",
    "local": "llama3",
}


async def _post_json(
    url: str,
    *,
    headers: Dict[str, str],
    body: Dict[str, Any],
    kind: str,
    timeout: float = 180.0,
):
    """POST to a provider endpoint through the SSRF guard.

    ``base_url`` is operator-configurable (Settings → AI, "Advanced/custom"), and
    the request carries the DECRYPTED provider API key in its headers. Posting to
    it unscreened made this the one runtime path that could be pointed at
    ``169.254.169.254`` or an internal service — reflecting the response back
    through AI-assist AND handing over the key. The "Test provider" button in
    routers/settings.py already went through safe_fetch; this is the same URL, so
    it gets the same treatment.

    ``safe_fetch`` also screens every redirect hop and strips credential headers
    if one crosses an origin, so a provider that answers 302 cannot walk off with
    the key either.

    The ``local`` provider kind is the deliberate exception: it exists to talk to
    an OpenAI-compatible server on the operator's own machine (Ollama, llama.cpp),
    so private addresses are expected there and only there.
    """
    from services.safe_fetch import safe_fetch

    return await safe_fetch(
        url,
        method="POST",
        headers=headers,
        json=body,
        timeout=timeout,
        allow_private=True if kind == "local" else None,
    )


async def _active_provider():
    """The highest-priority active provider row (lower priority = preferred)."""
    from sqlalchemy import select
    from models.ai_provider_config import AIProviderConfig
    async with AsyncSessionLocal() as db:
        row = (
            await db.execute(
                select(AIProviderConfig)
                .where(AIProviderConfig.is_active == True)  # noqa: E712
                .order_by(AIProviderConfig.priority.asc(), AIProviderConfig.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return row


async def has_local_provider() -> bool:
    """Whether the owner has configured at least one active AI provider."""
    return (await _active_provider()) is not None


def _decrypt(enc: Optional[str]) -> Optional[str]:
    if not enc:
        return None
    from security.encryption import SecretEncryption
    return SecretEncryption.decrypt_secret(enc)


# ── message-shape adapters ───────────────────────────────────────────────────
def _to_openai_messages(messages: List[Dict[str, Any]], system: Optional[str]) -> List[Dict[str, Any]]:
    """Convert generic/Anthropic-style messages to OpenAI chat format (system as a
    role, image blocks → data-URL image_url)."""
    out: List[Dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for b in content:
                bt = b.get("type")
                if bt == "text":
                    parts.append({"type": "text", "text": b.get("text", "")})
                elif bt == "image":
                    src = b.get("source", {}) or {}
                    media = src.get("media_type", "image/png")
                    data = src.get("data", "")
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}})
                elif bt == "image_url":
                    parts.append(b)
            out.append({"role": m.get("role", "user"), "content": parts})
        else:
            out.append({"role": m.get("role", "user"), "content": content or ""})
    return out


def _to_responses_input(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert generic messages to the OpenAI Responses `input` array."""
    out: List[Dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        parts = []
        if isinstance(content, list):
            for b in content:
                if b.get("type") == "text":
                    parts.append({"type": "input_text", "text": b.get("text", "")})
                elif b.get("type") == "image":
                    src = b.get("source", {}) or {}
                    media = src.get("media_type", "image/png")
                    parts.append({"type": "input_image", "image_url": f"data:{media};base64,{src.get('data', '')}"})
        else:
            parts.append({"type": "input_text", "text": content or ""})
        out.append({"role": m.get("role", "user"), "content": parts})
    return out


async def local_complete(
    messages: List[Dict[str, Any]],
    system: Optional[str] = None,
    max_tokens: int = 500,
    model: Optional[str] = None,
    purpose: str = "assist",
) -> Dict[str, Any]:
    """Run a completion against the owner-configured provider, in-process."""
    from services.ai_gateway_client import AIGatewayError  # local import avoids cycle

    provider = await _active_provider()
    if provider is None:
        raise AIGatewayError("No AI provider configured. Add one in Settings → AI.")

    api_key = _decrypt(provider.api_key_encrypted)
    if not api_key and provider.provider != "local":
        raise AIGatewayError(f"No API key configured for the {provider.provider} provider.")

    kind = provider.provider
    use_model = model or provider.model or _DEFAULT_MODEL.get(kind, "")

    try:
        if kind == "anthropic":
            # base_url (Advanced/custom) is the API base, e.g. https://proxy/anthropic;
            # append /v1/messages unless it already points at the messages endpoint.
            if provider.base_url:
                b = provider.base_url.rstrip("/")
                url = b if b.endswith("/messages") else f"{b}/v1/messages"
            else:
                url = "https://api.anthropic.com/v1/messages"
            body: Dict[str, Any] = {"model": use_model, "max_tokens": max_tokens, "messages": messages}
            if system:
                body["system"] = system
            resp = await _post_json(
                url,
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                body=body,
                kind=kind,
            )
            if resp.status_code != 200:
                raise AIGatewayError(f"Anthropic returned {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            usage = data.get("usage", {}) or {}
            return {"content": content, "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}, "model": data.get("model", use_model)}

        if kind in ("openai", "openrouter", "local"):
            base = (provider.base_url
                    or ("https://openrouter.ai/api/v1" if kind == "openrouter"
                        else "http://localhost:11434/v1" if kind == "local"
                        else "https://api.openai.com/v1")).rstrip("/")
            headers = {"content-type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            resp = await _post_json(
                f"{base}/chat/completions",
                headers=headers,
                body={"model": use_model, "max_tokens": max_tokens, "messages": _to_openai_messages(messages, system)},
                kind=kind,
            )
            if resp.status_code != 200:
                raise AIGatewayError(f"{kind} returned {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") or ""
            usage = data.get("usage", {}) or {}
            return {"content": content, "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}, "model": data.get("model", use_model)}

        if kind == "openai_responses":
            base = (provider.base_url or "https://api.openai.com/v1").rstrip("/")
            body = {"model": use_model, "max_output_tokens": max_tokens, "input": _to_responses_input(messages)}
            if system:
                body["instructions"] = system
            resp = await _post_json(
                f"{base}/responses",
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
                body=body,
                kind=kind,
            )
            if resp.status_code != 200:
                raise AIGatewayError(f"openai_responses returned {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            content = data.get("output_text", "") or ""
            if not content:
                for item in data.get("output", []):
                    if item.get("type") == "message":
                        for block in item.get("content", []):
                            if block.get("type") == "output_text":
                                content += block.get("text", "")
            usage = data.get("usage", {}) or {}
            return {"content": content, "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}, "model": data.get("model", use_model)}

        raise AIGatewayError(f"Unsupported AI provider: {kind}")
    except AIGatewayError:
        raise
    except Exception as e:  # network / parse — surface as a gateway error
        raise AIGatewayError(f"AI provider call failed: {e}") from e
