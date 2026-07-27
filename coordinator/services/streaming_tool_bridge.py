"""
Streaming tool bridge — OpenAI-style tool-calling over the browser chat bridge.

The streaming service exposes an OpenAI-compatible chat surface, but the actual
completion is produced by driving a real chat model through a browser handler
(``ps.page.*``) rather than by calling a provider API. Browser chat models don't
speak the OpenAI function-calling protocol, so this module bridges the two:

  1. Convert OpenAI tool/function definitions into single-line text instructions
     appended to the user message (``build_message_with_tools`` /
     ``format_tools_as_text``).
  2. Ask the model to emit tool invocations as fenced ``tool_call`` JSON blocks.
  3. Parse those blocks back out of the model's text response and re-materialize
     them as proper OpenAI-format ``tool_calls`` (``parse_tool_calls`` /
     ``build_tool_call_response``).
  4. Fold prior tool/function *results* back into the next turn as plain text
     context (``format_tool_results_as_text``).

It is model-agnostic — it works with any chat model that can follow instructions
and emit JSON on request — and it is pure logic (no I/O, no DB, no network), so it
is safe to call from anywhere in the request path.

``extract_attachments`` also lives here: it pulls inline image/file/audio/document
parts out of multimodal chat messages. Resolving ``file_id`` references (pointers
into the file library) is a separate, I/O-bearing concern handled by
``services.streaming_attachments``.
"""
import json
import re
import uuid
from typing import Optional, List, Tuple

# ── Tool definition → text prompt ──────────────────────────────────────────

# NOTE: No newlines (\n) anywhere in tool text — the browser runtime's
# ps.page.* functions use internal JSON serialization that breaks on
# control characters in the handler's scope data.
TOOL_SYSTEM_PROMPT = (
    "You have access to the following tools. "
    "To use a tool, respond with a JSON block in this exact format: "
    '```tool_call {"name": "tool_name", "arguments": {"param1": "value1"}} ``` '
    "You can make multiple tool calls in one response. Each must be in its own ```tool_call block. "
    "If you don't need to use any tools, just respond normally with text. "
    "Available tools: "
)

# Built-in model tools that are handled natively — not via text-based function calling
BUILTIN_TOOL_TYPES = {"image_generation", "web_search", "code_interpreter", "file_search", "computer"}


def format_tools_as_text(tools: List[dict]) -> str:
    """Convert OpenAI tool definitions to single-line human-readable text.

    Only function-calling tools get the text prompt. Built-in tools
    (image_generation, web_search, etc.) are handled natively by the
    model and are silently skipped.

    IMPORTANT: output must never contain newlines or other control characters.
    """
    if not tools:
        return ""

    # Separate function-calling tools from built-in tools
    function_tools = []
    for tool in tools:
        tool_type = tool.get("type", "")
        if tool_type == "function":
            function_tools.append(tool)
        elif tool_type in BUILTIN_TOOL_TYPES:
            continue  # skip — handled natively by the model
        elif "function" in tool or "name" in tool:
            function_tools.append(tool)  # legacy format

    if not function_tools:
        return ""

    parts = [TOOL_SYSTEM_PROMPT]
    for tool in function_tools:
        if tool.get("type") == "function":
            fn = tool.get("function", {})
        else:
            fn = tool  # Legacy function format

        name = fn.get("name", "unknown")
        desc = fn.get("description", "")
        params = fn.get("parameters", {})

        parts.append(f"**{name}**: {desc}.")

        props = params.get("properties", {})
        required = params.get("required", [])
        if props:
            param_descs = []
            for pname, pschema in props.items():
                ptype = pschema.get("type", "string")
                pdesc = pschema.get("description", "")
                req = " (required)" if pname in required else " (optional)"
                enum = f" — one of: {pschema['enum']}" if "enum" in pschema else ""
                param_descs.append(f"{pname} ({ptype}{req}): {pdesc}{enum}")
            parts.append("Parameters: " + "; ".join(param_descs) + ".")

    return " ".join(parts)


def format_tool_results_as_text(messages: List[dict]) -> str:
    """Convert tool result messages to text the model can understand.

    IMPORTANT: output must never contain newlines or other control characters.
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            name = msg.get("name", "tool")
            content = str(msg.get("content", "")).replace("\n", " ").replace("\r", "")
            parts.append(f"Tool result from {name} (call {tool_call_id}): {content}")
        elif role == "function":
            name = msg.get("name", "function")
            content = str(msg.get("content", "")).replace("\n", " ").replace("\r", "")
            parts.append(f"Function result from {name}: {content}")
    return " | ".join(parts)


# ── Build the full message to send to the model ───────────────────────────

def build_message_with_tools(
    messages: List[dict],
    tools: Optional[List[dict]] = None,
    functions: Optional[List[dict]] = None,
) -> str:
    """
    Build the final text message to send to the browser chat model.

    Handles:
    - Regular messages → extract last user message
    - Tool definitions → append as system instruction
    - Tool results → format as text context
    - Assistant messages with tool_calls → format as context
    """
    # Convert legacy functions to tools format
    effective_tools = tools or []
    if not effective_tools and functions:
        effective_tools = [{"type": "function", "function": f} for f in functions]

    # Extract conversation parts
    last_user_msg = ""
    tool_context_parts = []
    conversation_context = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            # Handle multimodal content (supports both "text" and "input_text" types for Responses API compat)
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("text", "input_text")]
                last_user_msg = " ".join(text_parts)
            elif isinstance(content, str):
                last_user_msg = content

        elif role == "tool" or role == "function":
            tool_context_parts.append(msg)

        elif role == "assistant":
            # If assistant had tool_calls, include the context
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    args = str(fn.get('arguments', '{}')).replace("\n", " ").replace("\r", "")
                    conversation_context.append(
                        f"[You previously called tool '{fn.get('name')}' with arguments: {args}]"
                    )
            elif content:
                # Regular assistant message — could be relevant context
                clean = str(content).replace("\n", " ").replace("\r", "")
                if len(clean) < 500:
                    conversation_context.append(f"[Your previous response: {clean}]")

        elif role == "system":
            # System messages become context
            if content:
                clean = str(content).replace("\n", " ").replace("\r", "")
                conversation_context.append(f"[System: {clean}]")

    # Build the final message — NEVER use newlines; the browser runtime's
    # ps.page.* functions break on control characters in serialized data.
    parts = []

    # Add tool definitions
    if effective_tools:
        tools_text = format_tools_as_text(effective_tools)
        if tools_text:
            parts.append(tools_text)

    # Add conversation context (system messages, previous tool calls)
    if conversation_context:
        parts.append(" ".join(conversation_context))

    # Add tool results
    if tool_context_parts:
        parts.append(format_tool_results_as_text(tool_context_parts))

    # Add the actual user message
    parts.append(last_user_msg)

    return " ".join(p for p in parts if p)


# ── Parse tool calls from model response ──────────────────────────────────

TOOL_CALL_PATTERN = re.compile(
    r'```tool_call\s*\n(.*?)\n```',
    re.DOTALL
)

# Also match common variations models might produce
TOOL_CALL_PATTERNS = [
    re.compile(r'```tool_call\s*\n(.*?)\n```', re.DOTALL),
    re.compile(r'```json\s*tool_call\s*\n(.*?)\n```', re.DOTALL),
    re.compile(r'<tool_call>\s*(.*?)\s*</tool_call>', re.DOTALL),
    # Match JSON objects that look like tool calls (name + arguments keys)
    re.compile(r'```(?:json)?\s*\n(\{[^}]*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{.*?\}\s*\})\n```', re.DOTALL),
]


def parse_tool_calls(response_text: str, available_tools: List[dict]) -> Tuple[Optional[List[dict]], str]:
    """
    Parse tool calls from the model's text response.

    Returns:
        (tool_calls, remaining_text)
        - tool_calls: list of OpenAI-format tool call objects, or None if no calls found
        - remaining_text: the response text with tool call blocks removed
    """
    if not available_tools or not response_text:
        return None, response_text

    # Build set of valid tool names
    valid_names = set()
    for tool in available_tools:
        if tool.get("type") == "function":
            valid_names.add(tool["function"]["name"])
        elif "name" in tool:
            valid_names.add(tool["name"])

    tool_calls = []
    remaining = response_text

    for pattern in TOOL_CALL_PATTERNS:
        for match in pattern.finditer(response_text):
            try:
                raw = match.group(1).strip()
                parsed = json.loads(raw)

                name = parsed.get("name", "")
                arguments = parsed.get("arguments", {})

                if name not in valid_names:
                    continue

                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments) if isinstance(arguments, dict) else str(arguments),
                    },
                })

                # Remove the matched block from remaining text
                remaining = remaining.replace(match.group(0), "").strip()

            except (json.JSONDecodeError, KeyError, AttributeError):
                continue

    if not tool_calls:
        return None, response_text

    return tool_calls, remaining


# ── Build OpenAI response with tool calls ─────────────────────────────────

def build_tool_call_response(
    completion_id: str,
    model: str,
    tool_calls: List[dict],
    remaining_text: str,
    created: int,
    usage: Optional[dict] = None,
) -> dict:
    """Build an OpenAI-format response containing tool calls.

    ``usage`` should carry real token counts (resolved by the caller);
    falls back to zeros only if the caller supplies nothing.
    """
    message = {
        "role": "assistant",
        "content": remaining_text if remaining_text else None,
        "tool_calls": tool_calls,
    }

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls",
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def has_tools(req) -> bool:
    """Check if the request includes tool/function definitions."""
    return bool(req.tools) or bool(req.functions)


def has_tool_results(messages: List[dict]) -> bool:
    """Check if messages contain tool/function results."""
    return any(m.get("role") in ("tool", "function") for m in messages)


# ── Attachment extraction ─────────────────────────────────────────────────

def extract_attachments(messages: List[dict]) -> List[dict]:
    """
    Extract all file/image attachments from messages.

    Returns list of dicts:
      - {type: "image", url: "data:...", mime: "image/png", name: "image.png"}
      - {type: "image", url: "https://...", mime: "image/jpeg", name: "photo.jpg"}
      - {type: "file", url: "data:...", mime: "application/pdf", name: "doc.pdf"}
      - {type: "file", content: "text content", mime: "text/plain", name: "file.txt"}
    """
    attachments = []

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for part in content:
            if not isinstance(part, dict):
                continue

            ptype = part.get("type", "")

            # Image URL (base64 or HTTP) — handles both Chat Completions ("image_url") and Responses API ("input_image")
            if ptype in ("image_url", "input_image"):
                img = part.get("image_url", {})
                url = img.get("url", "") if isinstance(img, dict) else str(img)
                if not url:
                    continue

                # Strip ALL non-base64 characters (control chars, MIME line breaks, etc.)
                if url.startswith("data:") and "," in url:
                    header, b64_data = url.split(",", 1)
                    b64_data = re.sub(r'[^A-Za-z0-9+/=]', '', b64_data)
                    url = f"{header},{b64_data}"

                mime = "image/png"
                name = "image.png"
                if url.startswith("data:"):
                    # data:image/jpeg;base64,...
                    header = url.split(",")[0] if "," in url else ""
                    if "image/jpeg" in header or "image/jpg" in header:
                        mime = "image/jpeg"
                        name = "image.jpg"
                    elif "image/gif" in header:
                        mime = "image/gif"
                        name = "image.gif"
                    elif "image/webp" in header:
                        mime = "image/webp"
                        name = "image.webp"
                elif "." in url.split("?")[0].split("/")[-1]:
                    name = url.split("?")[0].split("/")[-1]
                    ext = name.rsplit(".", 1)[-1].lower()
                    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                            "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml"}.get(ext, "image/png")

                attachments.append({"type": "image", "url": url, "mime": mime, "name": name})

            # File attachment
            elif ptype == "file":
                file_info = part.get("file", {})
                url = file_info.get("url", "")
                name = file_info.get("name", "file")
                mime = file_info.get("mime_type", "application/octet-stream")
                # Clean base64 data in file URLs
                if url.startswith("data:") and "," in url:
                    header, b64_data = url.split(",", 1)
                    b64_data = re.sub(r'[^A-Za-z0-9+/=]', '', b64_data)
                    url = f"{header},{b64_data}"
                attachments.append({"type": "file", "url": url, "mime": mime, "name": name})

            # Input audio
            elif ptype == "input_audio":
                audio = part.get("input_audio", {})
                data = audio.get("data", "")
                fmt = audio.get("format", "wav")
                # Clean base64 data
                if data:
                    data = re.sub(r'[^A-Za-z0-9+/=]', '', data)
                attachments.append({
                    "type": "audio",
                    "url": f"data:audio/{fmt};base64,{data}" if data else "",
                    "mime": f"audio/{fmt}",
                    "name": f"audio.{fmt}",
                })

            # Document (some providers)
            elif ptype == "document":
                doc = part.get("document", part)
                source = doc.get("source", {})
                if source.get("type") == "base64":
                    data = source.get("data", "")
                    mime = source.get("media_type", "application/pdf")
                    ext = mime.split("/")[-1]
                    # Clean base64 data
                    if data:
                        data = re.sub(r'[^A-Za-z0-9+/=]', '', data)
                    attachments.append({
                        "type": "file",
                        "url": f"data:{mime};base64,{data}",
                        "mime": mime,
                        "name": f"document.{ext}",
                    })

    return attachments


__all__ = [
    "TOOL_SYSTEM_PROMPT",
    "BUILTIN_TOOL_TYPES",
    "format_tools_as_text",
    "format_tool_results_as_text",
    "build_message_with_tools",
    "parse_tool_calls",
    "build_tool_call_response",
    "has_tools",
    "has_tool_results",
    "extract_attachments",
]
