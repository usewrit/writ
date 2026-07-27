"""
AI auto-repair for STREAMING workflows (advanced_script / callable functions).

The existing step-repair flow (routers/automation.py:trigger_manual_repair) hands a
failed workflow back to the AGENT, which re-derives broken recorded-STEP selectors
at runtime (intelligent mode) and reports `ai_repair_history` entries with
old_steps/new_steps. That path cannot help a STREAMING workflow: a streaming
workflow has no recorded steps to re-derive — its logic lives in the
`advanced_script` (a long-lived JS handler the buyer drives over MCP/functions) or
in a specific `functions[]` script entry. There is nothing for the
selector-rederivation loop to fix.

This module is the streaming branch of repair. Given a failed run it:
  1. Gathers the failing JS (the advanced_script, or the specific failing
     functions[] entry) + the runtime error + any console/network/DOM signal the
     agent reported on the task,
  2. Calls the SAME managed AI gateway the step-repair AI uses (agent_brain.call_ai)
     to produce a repaired script/function,
  3. VALIDATES the repaired JS parses/compiles BEFORE applying (never apply a
     broken script),
  4. Applies it by updating streaming_config.advanced_script (+ the authoritative
     `advanced_script` STEP in workflow.steps) or the matching functions[] entry,
  5. Records the repair via the SAME RepairHistory mechanism (workflow.ai_repair_history
     / repair_count / last_repaired_at), tagged repair_type="script" | "function".

Single-user coordinator: no AI token billing/metering.

SECURITY (never_trust_byo): repair rewrites the RECIPE, so it is OWNER-ONLY. No
agent is trusted for the repaired output: the JS is server-validated before it is
ever persisted.
"""
import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Cap how much of each signal we feed the model (token budget + prompt-injection
# blast radius). The error + the script are the load-bearing inputs.
_MAX_SCRIPT_CHARS = 24000
_MAX_ERROR_CHARS = 4000
_MAX_SIGNAL_CHARS = 6000
_MAX_OUTPUT_TOKENS = 4000

_SYSTEM_PROMPT = (
    "You are a senior JavaScript engineer repairing a browser-automation script that "
    "runs INSIDE a live page/browser context (it has access to `document`, `window`, "
    "the DOM, and any helper globals the recording runtime injects). The script FAILED "
    "at runtime. Given the current script, the runtime error, and any console/network/DOM "
    "signals, return a CORRECTED version of the SAME script that fixes the failure while "
    "preserving its original behaviour and its declared function entry points / exported "
    "handler names. Do NOT change function names, signatures, or the public callable "
    "surface. Do NOT add comments explaining the change. Return ONLY JSON of the exact "
    'shape: {"code": "<the full corrected script>", "explanation": "<one short sentence>"}'
    " . The `code` MUST be the COMPLETE replacement script, not a diff or a fragment."
)


def _truncate(s: Optional[str], n: int) -> str:
    if not s:
        return ""
    s = str(s)
    return s if len(s) <= n else (s[:n] + "\n...[truncated]...")


def _gather_signals(failed_task) -> str:
    """Pull any console/network/DOM signal the agent reported on the failed task.

    The agent reports these in task.result_data (advisory only — used purely as a
    diagnostic hint for the model, never trusted for success/billing)."""
    rd = getattr(failed_task, "result_data", None) or {}
    parts = []
    if isinstance(rd, dict):
        for key in ("console", "console_logs", "logs"):
            if rd.get(key):
                parts.append(f"CONSOLE:\n{_truncate(json.dumps(rd[key])[:_MAX_SIGNAL_CHARS], _MAX_SIGNAL_CHARS)}")
                break
        for key in ("network", "network_calls", "requests"):
            if rd.get(key):
                parts.append(f"NETWORK:\n{_truncate(json.dumps(rd[key])[:_MAX_SIGNAL_CHARS], _MAX_SIGNAL_CHARS)}")
                break
        for key in ("dom", "page_dom", "html"):
            if rd.get(key):
                parts.append(f"DOM:\n{_truncate(str(rd[key]), _MAX_SIGNAL_CHARS)}")
                break
        if rd.get("failed_function"):
            parts.append(f"FAILED FUNCTION (agent hint): {rd['failed_function']}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# JS validation — never apply a script that doesn't parse/compile.
# ---------------------------------------------------------------------------

async def validate_js(code: str) -> Tuple[bool, Optional[str]]:
    """Return (ok, error). Prefers `node --check` (real JS parser, SYNTAX-only — it
    does not execute the script or resolve browser globals), falling back to a
    structural balance check when node is unavailable.

    `node --check` is ideal here: it reports SyntaxError without running the code, so
    browser-only globals (document/window) never cause a false negative."""
    if not code or not code.strip():
        return False, "empty script"

    node = shutil.which("node")
    if node:
        try:
            # `node --check -` reads the script from stdin and only parses it.
            proc = await asyncio.create_subprocess_exec(
                node, "--check", "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(input=code.encode("utf-8")), timeout=10
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                return False, "validation timed out"
            if proc.returncode == 0:
                return True, None
            return False, _truncate(stderr.decode("utf-8", "replace"), 800)
        except Exception as e:  # node present but spawn failed — fall through
            logger.warning(f"[StreamingRepair] node --check failed to run, using fallback: {e}")

    # Fallback: structural balance + obvious-truncation check. Coarse, but it
    # rejects the common LLM failure modes (cut-off output, unbalanced braces).
    return _structural_js_check(code)


def _structural_js_check(code: str) -> Tuple[bool, Optional[str]]:
    """Dependency-free coarse JS sanity check (delimiter balance, string/comment
    awareness). Used only when node is unavailable."""
    pairs = {")": "(", "]": "[", "}": "{"}
    openers = set(pairs.values())
    stack = []
    i, n = 0, len(code)
    in_s = None  # current string/template delimiter
    in_line_comment = False
    in_block_comment = False
    while i < n:
        c = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if c == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_s:
            if c == "\\":
                i += 2
                continue
            if c == in_s:
                in_s = None
            i += 1
            continue
        # not in string/comment
        if c == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if c in ("'", '"', "`"):
            in_s = c
            i += 1
            continue
        if c in openers:
            stack.append(c)
        elif c in pairs:
            if not stack or stack[-1] != pairs[c]:
                return False, f"unbalanced '{c}' near offset {i}"
            stack.pop()
        i += 1
    if in_s:
        return False, "unterminated string/template literal"
    if in_block_comment:
        return False, "unterminated block comment"
    if stack:
        return False, f"unclosed '{stack[-1]}' delimiter(s)"
    return True, None


# ---------------------------------------------------------------------------
# Locate the failing script: advanced_script (default) or a functions[] entry.
# ---------------------------------------------------------------------------

def _read_advanced_script(workflow) -> str:
    """The authoritative advanced_script STEP wins; fall back to the
    streaming_config.advanced_script mirror (mirrors StreamingScriptEditor)."""
    for s in (workflow.steps or []):
        if isinstance(s, dict) and s.get("type") == "advanced_script":
            code = ((s.get("config") or {}).get("code")) or ""
            if code:
                return code
    sc = workflow.streaming_config or {}
    return ((sc.get("advanced_script") or {}).get("code")) or ""


def _find_function_entry(workflow, function_name: str):
    """Return the functions[] dict whose name matches (script-type callable)."""
    for fn in (workflow.functions or []):
        if isinstance(fn, dict) and fn.get("name") == function_name:
            return fn
    return None


def _apply_advanced_script(workflow, new_code: str) -> None:
    """Write the repaired code into BOTH the authoritative advanced_script STEP and
    the streaming_config.advanced_script mirror (keeps the two in sync exactly like
    StreamingScriptEditor's save). Preserves declared functions/persistent flags."""
    steps = list(workflow.steps or [])
    found_step = False
    for s in steps:
        if isinstance(s, dict) and s.get("type") == "advanced_script":
            cfg = dict(s.get("config") or {})
            cfg["code"] = new_code
            s["config"] = cfg
            found_step = True
    if found_step:
        # Re-assign so SQLAlchemy detects the JSONB mutation.
        workflow.steps = steps

    sc = dict(workflow.streaming_config or {})
    adv = dict(sc.get("advanced_script") or {})
    adv["code"] = new_code
    adv["enabled"] = True
    sc["advanced_script"] = adv
    workflow.streaming_config = sc


def _apply_function_code(workflow, function_name: str, new_code: str) -> bool:
    """Write the repaired code into the matching functions[] entry. Returns True if
    applied. Re-assigns the list so the JSONB mutation is tracked."""
    fns = list(workflow.functions or [])
    applied = False
    for fn in fns:
        if isinstance(fn, dict) and fn.get("name") == function_name:
            fn["code"] = new_code
            applied = True
    if applied:
        workflow.functions = fns
    return applied


def _append_repair_history(
    workflow,
    *,
    repair_type: str,
    old_code: str,
    new_code: str,
    error_message: Optional[str],
    explanation: Optional[str],
    target_name: Optional[str],
    now: datetime,
) -> None:
    """Append a repair entry using the SAME mechanism as step repair
    (workflow.ai_repair_history / repair_count / last_repaired_at). The entry shape
    is RepairHistory.tsx-compatible: it carries old_steps/new_steps for the existing
    renderer plus old_code/new_code + repair_type for the script/function renderer."""
    entry = {
        "repaired_at": now.isoformat(),
        "repair_type": repair_type,  # "script" | "function"
        "error_message": (error_message or "")[:1000],
        "explanation": (explanation or "")[:500],
        "target_name": target_name,
        "old_code": old_code,
        "new_code": new_code,
        # Keep the step renderer from breaking — empty step arrays render nothing.
        "old_steps": [],
        "new_steps": [],
        "failed_step_index": None,
    }
    history = list(workflow.ai_repair_history or [])
    history.append(entry)
    # Bound the stored history (mirrors the spirit of raw_replay caps).
    workflow.ai_repair_history = history[-50:]
    workflow.repair_count = (workflow.repair_count or 0) + 1
    workflow.last_repaired_at = now


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

class StreamingRepairError(Exception):
    """Raised with a user-facing message when a streaming repair cannot proceed."""


async def repair_streaming_workflow(
    db: AsyncSession,
    workflow,
    failed_task,
    *,
    function_name: Optional[str] = None,
    api_key: Optional[dict] = None,
) -> dict:
    """Repair the failing advanced_script (default) or a specific functions[] entry.

    Calls the managed AI gateway, validates the repaired JS, applies it, and records a
    RepairHistory entry. Does NOT commit — the caller owns the transaction (mirrors
    trigger_manual_repair). Returns a result dict.

    Raises StreamingRepairError (mapped to a 4xx by the caller) for user-facing
    failures (no script found, validation failed, etc.)."""
    from services.agent_brain import call_ai, loads_lenient
    from services import ai_limits

    # ---- 1. Locate the failing JS -------------------------------------------------
    if function_name:
        fn = _find_function_entry(workflow, function_name)
        if not fn:
            raise StreamingRepairError(f"Function '{function_name}' not found on this workflow.")
        if (fn.get("type") or "script") != "script" or not fn.get("code"):
            raise StreamingRepairError(
                f"Function '{function_name}' has no editable script to repair."
            )
        old_code = fn.get("code") or ""
        repair_type = "function"
        target_name = function_name
    else:
        old_code = _read_advanced_script(workflow)
        if not old_code.strip():
            raise StreamingRepairError(
                "This streaming workflow has no advanced script to repair."
            )
        repair_type = "script"
        target_name = None

    error_message = getattr(failed_task, "error_message", None)
    signals = _gather_signals(failed_task)

    # ---- 2. Build the prompt + call the SAME AI gateway ---------------------------
    user_parts = [
        f"WORKFLOW: {workflow.name}",
    ]
    if target_name:
        user_parts.append(f"FAILING FUNCTION: {target_name}")
    user_parts.append(f"RUNTIME ERROR:\n{_truncate(error_message, _MAX_ERROR_CHARS) or '(no error message captured)'}")
    if signals:
        user_parts.append(f"DIAGNOSTIC SIGNALS:\n{signals}")
    user_parts.append(f"CURRENT SCRIPT:\n{_truncate(old_code, _MAX_SCRIPT_CHARS)}")
    user_content = "\n\n".join(user_parts)

    messages = [{"role": "user", "content": user_content}]

    try:
        ai_text, input_tokens, output_tokens, ai_model = await call_ai(
            messages=messages,
            system_prompt=_SYSTEM_PROMPT,
            max_tokens=await ai_limits.resolve_max_tokens(db, "repair", _MAX_OUTPUT_TOKENS),
            purpose="repair",
        )
    except Exception:
        # call_ai already maps gateway errors to internal_http_error; re-raise.
        raise

    # ---- 3. Single-user coordinator: no AI token billing/metering. ----------------
    credits_used = 0
    ref_id = str(uuid.uuid4())

    # ---- 4. Parse + VALIDATE before applying --------------------------------------
    try:
        parsed = loads_lenient(ai_text)
    except Exception:
        raise StreamingRepairError(
            "AI did not return a usable repair (could not parse the response)."
        )
    new_code = (parsed.get("code") or "").strip()
    explanation = parsed.get("explanation") or ""
    if not new_code:
        raise StreamingRepairError("AI returned an empty repair; original script left unchanged.")
    if new_code == old_code.strip():
        raise StreamingRepairError("AI returned an identical script; nothing to repair.")

    ok, verr = await validate_js(new_code)
    if not ok:
        logger.info(
            f"[StreamingRepair] rejected invalid repaired JS for workflow "
            f"{workflow.id} (target={target_name or 'advanced_script'}): {verr}"
        )
        raise StreamingRepairError(
            f"AI repair did not compile; original script left unchanged ({verr})."
        )

    # ---- 5. Apply + record (RepairHistory mechanism) ------------------------------
    if repair_type == "function":
        applied = _apply_function_code(workflow, function_name, new_code)
        if not applied:
            raise StreamingRepairError(f"Function '{function_name}' could not be updated.")
    else:
        _apply_advanced_script(workflow, new_code)

    now = datetime.now(timezone.utc)
    _append_repair_history(
        workflow,
        repair_type=repair_type,
        old_code=old_code,
        new_code=new_code,
        error_message=error_message,
        explanation=explanation,
        target_name=target_name,
        now=now,
    )
    # Repairing the recipe clears the failure streak (the broken script is now fixed).
    workflow.consecutive_failures = 0

    # Mark the failed task as repair-attempted (mirrors the step-repair task flag).
    try:
        failed_task.ai_repair_attempted = True
        failed_task.ai_repair_result = {
            "success": True,
            "repair_type": repair_type,
            "target_name": target_name,
            "explanation": explanation[:500],
            "ref_id": ref_id,
        }
    except Exception:
        pass

    logger.info(
        f"[StreamingRepair] workflow {workflow.id}: repaired {repair_type} "
        f"(target={target_name or 'advanced_script'}) credits={credits_used} "
        f"in={input_tokens} out={output_tokens}"
    )

    return {
        "status": "repaired",
        "repair_type": repair_type,
        "target_name": target_name,
        "explanation": explanation,
        "credits_used": credits_used,
        "repair_count": workflow.repair_count,
    }
