"""Pure helper functions for the WhatsBot-Lite server."""

import json
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse


def _get_web_dir() -> Path:
    """Locate the web/ directory."""
    return Path(__file__).resolve().parent.parent / "web"


def _ok(data: Any = None) -> dict:
    return {"ok": True, "data": data}


def _err(message: str, status: int = 400, **extra: Any) -> JSONResponse:
    """Error response. ``extra`` adds machine-readable fields (e.g. ``error_code``)."""
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


def _mask_key(key: str) -> str:
    """Mask an API key for display (show first 8 + last 4 chars)."""
    if len(key) <= 12:
        return "*" * len(key)
    return key[:8] + "*" * (len(key) - 12) + key[-4:]


def _recover_multiple_arrays(text: str) -> list[str] | None:
    """Recover from a reply that is several JSON arrays glued together.

    The split_messages contract is ONE JSON array, but the model sometimes
    drifts and emits one array per message separated by ``---`` or blank lines
    (``["a"]\\n---\\n["b"]``). A plain ``json.loads`` fails on that, so the old
    fallback shipped the raw text — brackets and ``---`` included — as a single
    giant WhatsApp message. Here we walk consecutive JSON values with
    ``raw_decode`` and flatten every string we find, ignoring the separators in
    between. Returns ``None`` when the text isn't a clean run of arrays/strings,
    so the caller can fall back safely.
    """
    decoder = json.JSONDecoder()
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        # Skip whitespace and bare ``---`` separators between arrays.
        while i < n and (text[i].isspace() or text[i] == "-"):
            i += 1
        if i >= n:
            break
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            return None
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list) and all(isinstance(p, str) for p in value):
            out.extend(value)
        else:
            return None
        i = end
    filtered = [p.strip() for p in out if p.strip()]
    return filtered or None


def parse_split_reply(reply: str) -> list[str]:
    """Parse LLM reply as JSON array of strings. Fallback to single message."""
    text = reply.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]).strip()
    if text.startswith("["):
        try:
            parts = json.loads(text)
            if isinstance(parts, list) and all(isinstance(p, str) for p in parts):
                filtered = [p.strip() for p in parts if p.strip()]
                if filtered:
                    return filtered
        except (json.JSONDecodeError, TypeError):
            pass
        # Drifted output: multiple arrays glued with ``---``/newlines. Recover
        # the individual messages instead of shipping raw JSON to the user.
        recovered = _recover_multiple_arrays(text)
        if recovered:
            return recovered
    return [reply]
