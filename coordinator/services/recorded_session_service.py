"""Record-time pinned sessions: the RECORDING browser's auth state, kept on
the workflow it produced.

Recording happens in a real browser the user drives — they may clear a
captcha, accept a cookie wall, or sign in along the way. That state used to
die with the recording context, so the very first replay met every wall
again. This service pins it to the workflow at save time and hands it to run
dispatch as the LOWEST-precedence session seed:

    persona warm session  >  workflow+agent affinity  >  recorded session

A linked persona always wins (identity-scoped, actively refreshed); the
affinity row wins next (freshest agent-local state when session persistence
is on); the recorded snapshot is the seed of last resort — but it is exactly
what makes a captcha token or cookie-wall consent from the recording survive
into the first run.

Storage framing matches PersonaService exactly (json -> gzip -> b64 -> Fernet
with the coordinator secret key). The API only ever exposes
`has_recorded_session` + the capture timestamp, never the blob.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from security.encryption import SecretEncryption

logger = logging.getLogger(__name__)

# A session blob is cookies + storage, not a data store — cap hard so a
# hostile/bloated recording can't turn the workflow row into a blob column.
MAX_SERIALIZED_BYTES = 512 * 1024
MAX_COOKIES = 500
MAX_STORAGE_KEYS = 200
MAX_HEADERS = 50


class RecordedSessionService:
    """Pin / load / refresh the record-time session on a workflow row."""

    @staticmethod
    def sanitize(auth_session: Any) -> Optional[Dict[str, Any]]:
        """Whitelist + cap a client-supplied auth session; None when empty.

        The wizard posts this blob from the browser, so treat it as untrusted
        input: unknown keys are dropped, collections are size-capped, and a
        session with no cookies, storage, or headers pins nothing.
        """
        if not isinstance(auth_session, dict):
            return None
        out: Dict[str, Any] = {}
        cookies = auth_session.get("cookies")
        if isinstance(cookies, list) and cookies:
            out["cookies"] = [c for c in cookies[:MAX_COOKIES] if isinstance(c, dict)]
        for store in ("localStorage", "sessionStorage"):
            items = auth_session.get(store)
            if isinstance(items, dict) and items:
                out[store] = {
                    str(k): v for k, v in list(items.items())[:MAX_STORAGE_KEYS]
                    if isinstance(v, str)
                }
        headers = auth_session.get("headers")
        if isinstance(headers, dict) and headers:
            out["headers"] = {
                str(k): v for k, v in list(headers.items())[:MAX_HEADERS]
                if isinstance(v, str)
            }
        fp = auth_session.get("fingerprint")
        if isinstance(fp, dict) and fp:
            out["fingerprint"] = fp
        if not any(out.get(k) for k in ("cookies", "localStorage", "sessionStorage", "headers")):
            return None
        extracted_at = auth_session.get("extracted_at")
        if isinstance(extracted_at, str):
            out["extracted_at"] = extracted_at
        return out

    @staticmethod
    def pin(workflow, auth_session: Any) -> bool:
        """Sanitize + encrypt `auth_session` onto `workflow`; True when pinned.

        Sync and side-effect-only on the already-loaded row (no awaits, no lazy
        loads) — safe to call from any dispatch/completion context. The caller
        owns the commit.
        """
        clean = RecordedSessionService.sanitize(auth_session)
        if clean is None:
            return False
        raw = json.dumps(clean)
        # Cap the RAW payload (it rides dispatch frames decrypted) and the
        # stored blob (a highly-compressible payload must not bypass the bound).
        if len(raw) > MAX_SERIALIZED_BYTES:
            logger.warning(
                "recorded session for workflow %s exceeds %d raw bytes — not pinned",
                getattr(workflow, "id", "?"), MAX_SERIALIZED_BYTES,
            )
            return False
        compressed = base64.b64encode(gzip.compress(raw.encode())).decode()
        if len(compressed) > MAX_SERIALIZED_BYTES:
            logger.warning(
                "recorded session for workflow %s exceeds %d stored bytes — not pinned",
                getattr(workflow, "id", "?"), MAX_SERIALIZED_BYTES,
            )
            return False
        workflow.recorded_session_encrypted = SecretEncryption.encrypt_secret(compressed)
        workflow.recorded_session_captured_at = datetime.now(timezone.utc)
        return True

    @staticmethod
    def load(workflow) -> Optional[Dict[str, Any]]:
        """Decrypt the pinned session, or None. Never raises.

        Reads only plain columns of the already-loaded row, so it is safe in
        sync dispatch code. Individual cookie expiry is left to the browser —
        a partially-aged seed still beats a cold start.
        """
        blob = getattr(workflow, "recorded_session_encrypted", None)
        if not blob:
            return None
        try:
            compressed = SecretEncryption.decrypt_secret(blob)
            return json.loads(gzip.decompress(base64.b64decode(compressed)).decode())
        except Exception as e:
            logger.warning(
                "failed to decrypt recorded session for workflow %s: %s",
                getattr(workflow, "id", "?"), e,
            )
            return None

    @staticmethod
    def clear(workflow) -> None:
        workflow.recorded_session_encrypted = None
        workflow.recorded_session_captured_at = None
