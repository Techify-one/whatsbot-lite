"""Accent- and case-insensitive text matching shared by the app's searches.

No database access here: the conversation search and the file search both use
it, so typing "orcamento" finds "Orçamento" the same way on both screens.
"""

from __future__ import annotations

import unicodedata


def fold(s: str) -> str:
    """Casefold and strip accents so search matches regardless of diacritics.

    "Ó"/"ó"/"o" all fold to "o", so typing an unaccented letter still finds
    accented names (and vice-versa).
    """
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch)).casefold()


def match_snippet(content: str, folded_q: str, radius: int = 40) -> str:
    """A short excerpt of ``content`` centered on the first match of ``folded_q``,
    with ellipses when trimmed. Matching is accent/case-insensitive (via ``fold``),
    but the snippet keeps the ORIGINAL text (accents intact) for display.
    """
    if not content:
        return ""
    # Per-character fold, tracking the original index each folded char came from,
    # so a match position in the folded string maps back to the original text.
    folded_chars: list[str] = []
    orig_idx: list[int] = []
    for i, ch in enumerate(content):
        for fc in fold(ch):
            folded_chars.append(fc)
            orig_idx.append(i)
    folded = "".join(folded_chars)
    pos = folded.find(folded_q)
    if pos < 0:
        return content[: radius * 2].strip()
    start = orig_idx[pos]
    end_f = min(pos + len(folded_q), len(orig_idx)) - 1
    end = orig_idx[end_f] + 1 if end_f >= 0 else start
    w_start = max(0, start - radius)
    w_end = min(len(content), end + radius)
    excerpt = content[w_start:w_end].strip()
    prefix = "…" if w_start > 0 else ""
    suffix = "…" if w_end < len(content) else ""
    return f"{prefix}{excerpt}{suffix}"
