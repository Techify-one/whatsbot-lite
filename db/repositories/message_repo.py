"""Repository for messages table."""

from __future__ import annotations

import json
import time

from sqlalchemy import and_, delete as sa_delete, func, insert as sa_insert, or_, select, update as sa_update

from db.engine import get_engine
from db.tables import messages


def add(contact_id: int, role: str, content: str, *,
        media_type: str | None = None, media_path: str | None = None,
        status: str | None = None, msg_id: str | None = None,
        reply_to_msg_id: str | None = None,
        media_state: str | None = None,
        media_name: str | None = None,
        ts: float | None = None) -> dict:
    """Insert a message and return it as a dict.

    ``media_state`` is ``"discarded"`` when the file was deliberately not kept
    (auto-download policy); the row then has ``media_type`` but no ``media_path``.
    ``media_name`` is the name the sender gave the file ("Teste.mp4"), already
    cleaned by ``file_manager.clean_name``; ``None`` when none is known.
    """
    ts = ts or time.time()
    with get_engine().begin() as conn:
        result = conn.execute(sa_insert(messages).values(
            contact_id=contact_id,
            role=role,
            content=content,
            ts=ts,
            media_type=media_type,
            media_path=media_path,
            status=status,
            msg_id=msg_id,
            reply_to_msg_id=reply_to_msg_id,
            media_state=media_state,
            media_name=media_name,
        ))
        new_id = result.inserted_primary_key[0]
    return {
        "id": new_id,
        "role": role,
        "content": content,
        "ts": ts,
        "media_type": media_type,
        "media_path": media_path,
        "status": status,
        "msg_id": msg_id,
        "reply_to_msg_id": reply_to_msg_id,
    }


def get_all(contact_id: int) -> list[dict]:
    """Return all messages for a contact ordered by timestamp."""
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(messages)
            .where(messages.c.contact_id == contact_id)
            .order_by(messages.c.ts, messages.c.id)
        ).mappings().all()
    return [_row_to_dict(r) for r in rows]


# ── Cursor pagination ─────────────────────────────────────────────────
# Messages are ordered by (ts, id). The DB id is the cursor handed to the
# client (`_id`), and ts breaks ties deterministically, so two messages that
# share a timestamp are never skipped or repeated between pages.

def _cursor_of(conn, contact_id: int, message_id: int) -> tuple[float, int] | None:
    row = conn.execute(
        select(messages.c.ts, messages.c.id).where(
            (messages.c.id == message_id) & (messages.c.contact_id == contact_id)
        )
    ).first()
    return (row.ts, row.id) if row else None


def _page_older(conn, contact_id: int, cursor, limit: int):
    """Up to ``limit`` messages strictly older than ``cursor`` (ascending), plus
    whether even older ones remain. ``cursor=None`` means "from the newest"."""
    stmt = select(messages).where(messages.c.contact_id == contact_id)
    if cursor is not None:
        ts, mid = cursor
        stmt = stmt.where(or_(messages.c.ts < ts, and_(messages.c.ts == ts, messages.c.id < mid)))
    rows = conn.execute(
        stmt.order_by(messages.c.ts.desc(), messages.c.id.desc()).limit(limit + 1)
    ).mappings().all()
    has_more = len(rows) > limit
    rows = list(rows[:limit])
    rows.reverse()
    return rows, has_more


def _page_newer(conn, contact_id: int, cursor, limit: int):
    """Up to ``limit`` messages strictly newer than ``cursor`` (ascending), plus
    whether even newer ones remain."""
    ts, mid = cursor
    rows = conn.execute(
        select(messages)
        .where(
            (messages.c.contact_id == contact_id)
            & or_(messages.c.ts > ts, and_(messages.c.ts == ts, messages.c.id > mid))
        )
        .order_by(messages.c.ts, messages.c.id)
        .limit(limit + 1)
    ).mappings().all()
    has_more = len(rows) > limit
    return list(rows[:limit]), has_more


def find_id_by_msg_id(contact_id: int, msg_id: str) -> int | None:
    """DB id of a contact's message given its GOWA msg_id (None when unknown)."""
    if not msg_id:
        return None
    with get_engine().connect() as conn:
        return conn.execute(
            select(messages.c.id)
            .where((messages.c.contact_id == contact_id) & (messages.c.msg_id == msg_id))
            .limit(1)
        ).scalar_one_or_none()


def get_page(contact_id: int, limit: int = 60, *, before: int | None = None,
             after: int | None = None, around: int | None = None) -> dict:
    """One page of a contact's history, ascending, with cursor metadata.

    - no cursor: the newest ``limit`` messages
    - ``before=<id>``: the ``limit`` messages older than that one
    - ``after=<id>``: the ``limit`` messages newer than that one
    - ``around=<id>``: that message with about half a page on each side

    Returns ``{"messages", "has_more_before", "has_more_after", "found"}``.
    ``found`` is False when a cursor id does not belong to this contact; the
    caller decides whether that is an error (``before``/``after``) or a
    fallback to the newest page (``around``).
    """
    limit = max(1, int(limit))
    with get_engine().connect() as conn:
        if around is not None:
            cur = _cursor_of(conn, contact_id, around)
            if cur is None:
                return {"messages": [], "has_more_before": False,
                        "has_more_after": False, "found": False}
            half = max(1, limit // 2)
            older, more_before = _page_older(conn, contact_id, cur, half)
            target = conn.execute(
                select(messages).where(messages.c.id == around)
            ).mappings().all()
            newer, more_after = _page_newer(conn, contact_id, cur, half)
            rows = [*older, *target, *newer]
        elif before is not None:
            cur = _cursor_of(conn, contact_id, before)
            if cur is None:
                return {"messages": [], "has_more_before": False,
                        "has_more_after": False, "found": False}
            rows, more_before = _page_older(conn, contact_id, cur, limit)
            more_after = True  # the cursor message itself is newer
        elif after is not None:
            cur = _cursor_of(conn, contact_id, after)
            if cur is None:
                return {"messages": [], "has_more_before": False,
                        "has_more_after": False, "found": False}
            rows, more_after = _page_newer(conn, contact_id, cur, limit)
            more_before = True
        else:
            rows, more_before = _page_older(conn, contact_id, None, limit)
            more_after = False
    return {
        "messages": [_row_to_dict(r) for r in rows],
        "has_more_before": more_before,
        "has_more_after": more_after,
        "found": True,
    }


def get_quoted(contact_id: int, page: list[dict]) -> dict[str, dict]:
    """Messages cited by ``page`` that are NOT part of it, keyed by GOWA msg_id.

    A paginated window may hold a reply whose original lives outside it; the
    client needs the original to render the quote and to jump to it.
    """
    present = {m.get("msg_id") for m in page if m.get("msg_id")}
    wanted = {
        m["reply_to_msg_id"] for m in page
        if m.get("reply_to_msg_id") and m["reply_to_msg_id"] not in present
    }
    if not wanted:
        return {}
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(messages).where(
                (messages.c.contact_id == contact_id) & messages.c.msg_id.in_(sorted(wanted))
            )
        ).mappings().all()
    return {r["msg_id"]: _row_to_dict(r) for r in rows}


def get_context(contact_id: int, limit: int) -> list[dict]:
    """Return the last N eligible messages for LLM context."""
    # `system` = system-event messages (e.g. improvement analyses) that signal
    # something happened but are NOT meant for the AI to read. Kept out of the
    # LLM context here so they never leak into a reply.
    excluded = ("transcription", "tool_call", "system_notice", "system")
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(messages)
            .where(
                (messages.c.contact_id == contact_id)
                & (~messages.c.role.in_(excluded))
                & ((messages.c.status.is_(None)) | (messages.c.status != "failed"))
            )
            .order_by(messages.c.ts.desc())
            .limit(limit)
        ).mappings().all()
    return [_row_to_dict(r) for r in reversed(rows)]


def get_last(contact_id: int) -> dict | None:
    """Return the most recent message for a contact."""
    with get_engine().connect() as conn:
        row = conn.execute(
            select(messages)
            .where(messages.c.contact_id == contact_id)
            .order_by(messages.c.ts.desc())
            .limit(1)
        ).mappings().first()
    return _row_to_dict(row) if row else None


def get_last_user_message(contact_id: int) -> dict | None:
    """Return the most recent user message (for updating with transcription etc)."""
    with get_engine().connect() as conn:
        row = conn.execute(
            select(messages)
            .where((messages.c.contact_id == contact_id) & (messages.c.role == "user"))
            .order_by(messages.c.ts.desc())
            .limit(1)
        ).mappings().first()
    return _row_to_dict(row) if row else None


def update_content(message_id: int, content: str) -> None:
    """Update the content of a specific message."""
    with get_engine().begin() as conn:
        conn.execute(sa_update(messages).where(messages.c.id == message_id).values(content=content))


def update_status(contact_id: int, content: str, new_status: str | None,
                   msg_id: str | None = None) -> None:
    """Update status of the most recent message matching content (for retry-send)."""
    with get_engine().begin() as conn:
        target_id = conn.execute(
            select(messages.c.id)
            .where(
                (messages.c.contact_id == contact_id)
                & (messages.c.content == content)
                & (messages.c.status == "failed")
            )
            .order_by(messages.c.ts.desc())
            .limit(1)
        ).scalar_one_or_none()
        if target_id is None:
            return
        values = {"status": new_status}
        if msg_id:
            values["msg_id"] = msg_id
        conn.execute(sa_update(messages).where(messages.c.id == target_id).values(**values))


def update_status_by_msg_id(msg_id: str, new_status: str) -> list[str]:
    """Update delivery status by GOWA msg_id. Forward-only: sent → delivered → read.

    Does not overwrite 'operator' or 'failed' statuses. Cascades to all prior
    outgoing messages for the same contact (delivered/read are monotonic).
    """
    updated_msg_ids: list[str] = []
    with get_engine().begin() as conn:
        # Update the specific message.
        result = conn.execute(
            sa_update(messages)
            .where(
                (messages.c.msg_id == msg_id)
                & (messages.c.status.is_not(None))
                & (messages.c.status.in_(("sent", "delivered")))
            )
            .values(status=new_status)
        )
        if (result.rowcount or 0) > 0:
            updated_msg_ids.append(msg_id)

        # Find the anchor row to drive the cascade.
        anchor = conn.execute(
            select(messages.c.contact_id, messages.c.ts).where(messages.c.msg_id == msg_id)
        ).first()
        if anchor:
            prior_statuses = ("sent",) if new_status == "delivered" else ("sent", "delivered")
            # Gather IDs first so we can return them to the caller.
            prior_rows = conn.execute(
                select(messages.c.msg_id)
                .where(
                    (messages.c.contact_id == anchor.contact_id)
                    & (messages.c.role == "assistant")
                    & (messages.c.ts <= anchor.ts)
                    & (messages.c.status.in_(prior_statuses))
                    & (messages.c.msg_id.is_not(None))
                    & (messages.c.msg_id != msg_id)
                )
            ).all()
            cascaded = [r.msg_id for r in prior_rows]
            if cascaded:
                conn.execute(
                    sa_update(messages)
                    .where(
                        (messages.c.contact_id == anchor.contact_id)
                        & (messages.c.role == "assistant")
                        & (messages.c.ts <= anchor.ts)
                        & (messages.c.status.in_(prior_statuses))
                    )
                    .values(status=new_status)
                )
                updated_msg_ids.extend(cascaded)
    return updated_msg_ids


def get_contact_id_by_msg_id(msg_id: str) -> int | None:
    """Look up the contact_id for a given GOWA msg_id."""
    with get_engine().connect() as conn:
        cid = conn.execute(
            select(messages.c.contact_id).where(messages.c.msg_id == msg_id).limit(1)
        ).scalar_one_or_none()
    return cid


def update_msg_id_and_status(message_id: int, msg_id: str, status: str) -> None:
    """Set msg_id and status on a message (used after retry-send)."""
    with get_engine().begin() as conn:
        conn.execute(
            sa_update(messages).where(messages.c.id == message_id).values(msg_id=msg_id, status=status)
        )


def delete_all(contact_id: int) -> None:
    """Delete all messages for a contact."""
    with get_engine().begin() as conn:
        conn.execute(sa_delete(messages).where(messages.c.contact_id == contact_id))


def get_by_msg_id(msg_id: str) -> dict | None:
    """Look up a single message by its GOWA msg_id."""
    if not msg_id:
        return None
    with get_engine().connect() as conn:
        row = conn.execute(
            select(messages).where(messages.c.msg_id == msg_id).limit(1)
        ).mappings().first()
    return _row_to_dict(row) if row else None


# `revoked` column encodes WHICH kind of deletion happened, so the UI can show a
# scope-specific label. 0 = not revoked, 1 = "para todos" (delete for everyone),
# 2 = "para mim" (delete for me). Old rows revoked before this distinction existed
# carry 1 and read as "para todos".
_REVOKE_CODE = {"all": 1, "me": 2}


def mark_revoked(msg_id: str, scope: str = "all") -> bool:
    """Mark a message as revoked by its GOWA msg_id. Covers both 'delete for me'
    (scope='me') and 'delete for everyone' (scope='all') — in both cases the message
    is removed from WhatsApp but KEPT in our DB (content/media preserved) so the panel
    still shows it with a scope-specific 'deleted' indicator. Returns True if matched."""
    if not msg_id:
        return False
    with get_engine().begin() as conn:
        result = conn.execute(
            sa_update(messages)
            .where(messages.c.msg_id == msg_id)
            .values(revoked=_REVOKE_CODE.get(scope, 1))
        )
    return (result.rowcount or 0) > 0


def mark_revoked_by_id(message_id: int, scope: str = "me") -> bool:
    """Mark a message as revoked by its DB primary key (for local messages without a
    msg_id, e.g. failed sends / private notes). Content is kept; the row is never
    hard-deleted. Returns True if a row matched."""
    if not message_id:
        return False
    with get_engine().begin() as conn:
        result = conn.execute(
            sa_update(messages).where(messages.c.id == message_id)
            .values(revoked=_REVOKE_CODE.get(scope, 2))
        )
    return (result.rowcount or 0) > 0


def set_reaction(msg_id: str, emoji: str, reactor: str) -> dict | None:
    """Set (or clear, when ``emoji`` is empty) ``reactor``'s reaction on a message.

    Each reactor holds at most one emoji (WhatsApp semantics): a new emoji replaces
    the prior one. Returns the updated ``{emoji: [reactor, ...]}`` map, or None if
    the message doesn't exist.
    """
    if not msg_id:
        return None
    with get_engine().begin() as conn:
        row = conn.execute(
            select(messages.c.id, messages.c.reactions).where(messages.c.msg_id == msg_id).limit(1)
        ).mappings().first()
        if row is None:
            return None
        try:
            data = json.loads(row["reactions"]) if row["reactions"] else {}
        except (ValueError, TypeError):
            data = {}
        # Drop the reactor from every emoji (one reaction per person).
        for em in list(data.keys()):
            data[em] = [r for r in data[em] if r != reactor]
            if not data[em]:
                del data[em]
        if emoji:
            data.setdefault(emoji, [])
            if reactor not in data[emoji]:
                data[emoji].append(reactor)
        conn.execute(
            sa_update(messages).where(messages.c.id == row["id"]).values(
                reactions=json.dumps(data) if data else None
            )
        )
    return data


def _row_to_dict(row) -> dict:
    d = {
        "role": row["role"],
        "content": row["content"],
        "ts": row["ts"],
        "status": row["status"],
        "msg_id": row["msg_id"],
    }
    if row["media_type"]:
        d["media_type"] = row["media_type"]
    if row["media_path"]:
        d["media_path"] = row["media_path"]
    # `media_state` may be absent on rows read before the column existed.
    if row.get("media_state"):
        d["media_state"] = row["media_state"]
    # `revoked` may be absent on very old rows read before the column existed.
    # 1 = "para todos", 2 = "para mim" (see _REVOKE_CODE).
    if row.get("revoked"):
        d["revoked"] = True
        d["revoke_scope"] = "me" if row["revoked"] == 2 else "all"
    if row.get("reactions"):
        try:
            parsed = json.loads(row["reactions"])
            if parsed:
                d["reactions"] = parsed
        except (ValueError, TypeError):
            pass
    # `reply_to_msg_id` may be absent on old rows read before the column existed.
    if row.get("reply_to_msg_id"):
        d["reply_to_msg_id"] = row["reply_to_msg_id"]
    d["_id"] = row["id"]
    return d


# ── Media files (download policy + file manager) ──────────────────────

def set_media(message_id: int, media_path: str | None, media_state: str | None) -> None:
    """Point a message at a (re)downloaded file, or flag it as discarded/removed."""
    with get_engine().begin() as conn:
        conn.execute(
            sa_update(messages).where(messages.c.id == message_id)
            .values(media_path=media_path, media_state=media_state)
        )

def mark_media_removed(paths: list[str]) -> int:
    """Flag every message that points at one of ``paths`` as ``removed``."""
    return mark_media_state(paths, "removed")

def mark_media_state(paths: list[str], state: str) -> int:
    """Set ``media_state`` on every message that points at one of ``paths``.

    The path is cleared too, so the chat shows a placeholder instead of a broken
    image. Returns how many messages were updated.
    """
    paths = [p for p in dict.fromkeys(paths) if p]
    if not paths:
        return 0
    updated = 0
    with get_engine().begin() as conn:
        # Chunked: SQLite caps the number of bound parameters per statement.
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            result = conn.execute(
                sa_update(messages)
                .where(messages.c.media_path.in_(chunk))
                .values(media_path=None, media_state=state)
            )
            updated += result.rowcount or 0
    return updated

def paths_with_messages(paths: list[str]) -> dict[str, dict]:
    """Map each known ``media_path`` in ``paths`` to its message and contact.

    Used by the file manager to show where a file came from and to link to the
    exact message: ``contact_id``/``message_id`` are the ``contacts.id`` and
    ``messages.id`` the conversation view opens by. Paths without a message are
    simply absent (orphans). When more than one message points at a file, the
    oldest one wins (the one that actually brought the media in).
    """
    from db.tables import contacts  # local: keeps this module's import graph flat

    paths = [p for p in dict.fromkeys(paths) if p]
    found: dict[str, dict] = {}
    if not paths:
        return found
    with get_engine().connect() as conn:
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            rows = conn.execute(
                select(
                    messages.c.media_path, messages.c.media_type, messages.c.ts,
                    messages.c.msg_id, messages.c.id.label("message_id"),
                    contacts.c.id.label("contact_id"), contacts.c.phone, contacts.c.name,
                    contacts.c.is_group, contacts.c.group_name,
                )
                .select_from(messages.join(contacts, contacts.c.id == messages.c.contact_id))
                .where(messages.c.media_path.in_(chunk))
                .order_by(messages.c.id)
            ).mappings().all()
            for r in rows:
                found.setdefault(r["media_path"], dict(r))
    return found

def media_name_rows(paths: list[str]) -> list[dict]:
    """Where the name a sender gave each file in ``paths`` can be read from.

    One row per message that points at a path and could carry a name, oldest
    first: ``media_name`` (set since the column exists) and, for documents only,
    the head of ``content`` (``[Documento recebido: nome.ext]``, how the name was
    kept before). The caller picks the first usable one per path. ``content`` is
    cut short on purpose: this runs over whole pages of files.
    """
    paths = [p for p in dict.fromkeys(paths) if p]
    rows: list[dict] = []
    if not paths:
        return rows
    with get_engine().connect() as conn:
        for i in range(0, len(paths), 500):
            chunk = paths[i:i + 500]
            found = conn.execute(
                select(
                    messages.c.media_path, messages.c.media_name, messages.c.media_type,
                    func.substr(messages.c.content, 1, 400).label("content_head"),
                )
                .where(messages.c.media_path.in_(chunk))
                .where(or_(messages.c.media_name.is_not(None), messages.c.media_type == "document"))
                .order_by(messages.c.id)
            ).mappings().all()
            rows.extend(dict(r) for r in found)
    return rows

def media_message_rows(content_chars: int = 400) -> list[dict]:
    """Every message that points at a file under ``statics/``, oldest first.

    Feeds the file manager's database-backed filters (conversation, private or
    group, text search) in ONE query: those filters must look at every file, and
    a lookup per page of 500 paths would cost dozens of queries on a big folder.
    ``content`` is cut to ``content_chars`` (captions, transcriptions and
    descriptions can be long); pass a small number when the text is not needed.
    """
    from db.tables import contacts  # local: keeps this module's import graph flat

    with get_engine().connect() as conn:
        rows = conn.execute(
            select(
                messages.c.media_path, messages.c.media_name, messages.c.media_type,
                messages.c.id.label("message_id"), messages.c.ts,
                func.substr(messages.c.content, 1, content_chars).label("content"),
                contacts.c.id.label("contact_id"), contacts.c.phone, contacts.c.name,
                contacts.c.is_group, contacts.c.group_name,
            )
            .select_from(messages.join(contacts, contacts.c.id == messages.c.contact_id))
            .where(messages.c.media_path.like("statics/%"))
            .order_by(messages.c.id)
        ).mappings().all()
    return [dict(r) for r in rows]

def get_media_target(contact_id: int, msg_id: str) -> dict | None:
    """The message row (id, media_type, media_state) a Baixar click refers to."""
    if not msg_id:
        return None
    with get_engine().connect() as conn:
        row = conn.execute(
            select(messages.c.id, messages.c.media_type, messages.c.media_path,
                   messages.c.media_state, messages.c.role)
            .where((messages.c.contact_id == contact_id) & (messages.c.msg_id == msg_id))
            .limit(1)
        ).mappings().first()
    return dict(row) if row else None
