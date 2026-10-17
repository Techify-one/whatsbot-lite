"""Repository for contacts table."""

from __future__ import annotations

import time

from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy import select
from sqlalchemy import update as sa_update

from db.engine import get_engine
from db.tables import contact_tags, contacts, observations, tags, unread_msg_ids
from db.textmatch import fold as _fold, match_snippet as _match_snippet


def _br_phone_variants(phone: str) -> list[str]:
    """Return phone number variants for Brazilian numbers.

    BR mobile numbers can have 8 or 9 local digits:
    - 13 digits: 55 + 2-digit DDD + 9 + 8 digits (user-typed format)
    - 12 digits: 55 + 2-digit DDD + 8 digits (WhatsApp canonical format)
    """
    if not phone or not phone.startswith("55"):
        return [phone]
    if len(phone) == 13 and phone[4] == "9":
        alt = phone[:4] + phone[5:]
        return [phone, alt]
    if len(phone) == 12:
        alt = phone[:4] + "9" + phone[4:]
        return [phone, alt]
    return [phone]


def get_or_create(phone: str, default_ai_enabled: bool = True) -> dict:
    """Get a contact by phone, creating it if it doesn't exist."""
    variants = _br_phone_variants(phone)
    with get_engine().begin() as conn:
        row = conn.execute(
            select(contacts).where(contacts.c.phone.in_(variants))
        ).mappings().first()
        if row is not None:
            return _row_to_dict(row)
        now = time.time()
        result = conn.execute(sa_insert(contacts).values(
            phone=phone,
            ai_enabled=1 if default_ai_enabled else 0,
            created_at=now,
            updated_at=now,
        ))
        new_id = result.inserted_primary_key[0]
    return {
        "id": new_id,
        "phone": phone,
        "name": "",
        "email": "",
        "profession": "",
        "company": "",
        "address": "",
        "ai_enabled": default_ai_enabled,
        "is_group": False,
        "group_name": "",
        "is_archived": False,
        "archived_by_app": False,
        "can_send": True,
        "unread_count": 0,
        "unread_ai_count": 0,
        "created_at": now,
        "updated_at": now,
    }


def delete(contact_id: int) -> None:
    """Delete a contact and all related data (CASCADE handles child tables)."""
    with get_engine().begin() as conn:
        conn.execute(sa_delete(contacts).where(contacts.c.id == contact_id))


def set_archived(contact_id: int, archived: bool, by_app: bool = False) -> None:
    """Set the archived status of a contact."""
    with get_engine().begin() as conn:
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(
            is_archived=1 if archived else 0,
            archived_by_app=1 if (archived and by_app) else 0,
            updated_at=time.time(),
        ))


def set_pinned(contact_id: int, pinned: bool) -> None:
    """Pin or unpin a conversation (pinned ones sort to the top of the list)."""
    with get_engine().begin() as conn:
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(
            is_pinned=1 if pinned else 0,
            updated_at=time.time(),
        ))


def display_names(phones: list[str]) -> dict[str, dict]:
    """Display info for contacts, keyed by the phone/JID asked for.

    Only the columns the file manager needs; nothing is created. Unknown
    phones are simply absent.
    """
    phones = [p for p in dict.fromkeys(phones) if p]
    found: dict[str, dict] = {}
    if not phones:
        return found
    with get_engine().connect() as conn:
        for i in range(0, len(phones), 500):
            rows = conn.execute(
                select(contacts.c.phone, contacts.c.name, contacts.c.is_group, contacts.c.group_name)
                .where(contacts.c.phone.in_(phones[i:i + 500]))
            ).mappings().all()
            for r in rows:
                label = (r["group_name"] if r["is_group"] else r["name"]) or ""
                found[r["phone"]] = {"name": label.lstrip("~").strip(), "is_group": bool(r["is_group"])}
    return found

def _matching_rows(q: str) -> list[dict]:
    """The contact/group rows that ``q`` names (see :func:`search_ids` for the rules)."""
    folded = _fold((q or "").strip())
    if not folded:
        return []
    digits = "".join(ch for ch in q if ch.isdigit())
    # "(11) 99999-0001" is a number; "Grupo 2" is a name that happens to hold a digit.
    by_number = len(digits) >= 3 and not any(ch.isalpha() for ch in q)
    wanted: list[str] = []
    if by_number:
        # Typed with or without the country code, with or without the extra 9.
        for base in dict.fromkeys([digits, digits if digits.startswith("55") else "55" + digits]):
            wanted.extend(_br_phone_variants(base))
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(contacts.c.id, contacts.c.phone, contacts.c.name,
                   contacts.c.is_group, contacts.c.group_name, contacts.c.updated_at)
        ).mappings().all()
    hits: list[dict] = []
    for r in rows:
        if r["is_group"]:
            hit = folded in _fold(r["group_name"] or "")
        else:
            hit = (folded in _fold(r["name"] or "") or folded in r["phone"]
                   or any(d in r["phone"] for d in wanted))
        if hit:
            hits.append(dict(r))
    return hits

def search_ids(q: str) -> set[int]:
    """Ids of the contacts and groups that ``q`` names.

    Matches the contact name or the group name (accent/case-insensitive) and, for
    people, the phone number: a number typed the way people write it, with spaces,
    parentheses or dashes, finds the contact, and so does the Brazilian number
    with or without the extra 9. A group's own id is never matched by digits (it
    is an internal code, not something anybody types). Nothing is created.
    """
    return {r["id"] for r in _matching_rows(q)}

def suggest(q: str, limit: int = 8) -> list[dict]:
    """Contacts and groups that ``q`` names, for a "pick the conversation" list.

    Same matching as :func:`search_ids`, so what the list offers is exactly what
    the filter then finds. Each hit carries what is needed to tell two people
    with the same name apart (the phone number) plus the label the app shows
    elsewhere; people whose name starts with ``~`` (taken from WhatsApp) lose it.
    Best matches first (name starting with the text), then the most recently
    active. Nothing is created.
    """
    hits = _matching_rows(q)
    if not hits:
        return []
    folded = _fold((q or "").strip())

    def label(r: dict) -> str:
        return ((r["group_name"] if r["is_group"] else r["name"]) or "").lstrip("~").strip()

    hits.sort(key=lambda r: (
        0 if _fold(label(r)).startswith(folded) else 1,
        -(r["updated_at"] or 0),
    ))
    return [
        {
            "id": r["id"],
            "phone": r["phone"],
            "name": label(r),
            "is_group": bool(r["is_group"]),
        }
        for r in hits[:max(1, limit)]
    ]

def get_by_phone(phone: str) -> dict | None:
    """Get a contact by phone number. Checks BR phone variants."""
    variants = _br_phone_variants(phone)
    with get_engine().connect() as conn:
        row = conn.execute(
            select(contacts).where(contacts.c.phone.in_(variants))
        ).mappings().first()
    return _row_to_dict(row) if row else None


def update(contact_id: int, **fields) -> None:
    """Update specific fields on a contact."""
    if not fields:
        return
    fields["updated_at"] = time.time()
    with get_engine().begin() as conn:
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(**fields))


def increment_unread(contact_id: int, msg_id: str | None = None) -> None:
    """Increment unread_count and optionally track the msg_id."""
    with get_engine().begin() as conn:
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(
            unread_count=contacts.c.unread_count + 1,
            updated_at=time.time(),
        ))
        if msg_id:
            conn.execute(sa_insert(unread_msg_ids).values(
                contact_id=contact_id, msg_id=msg_id,
            ))


def increment_unread_ai(contact_id: int) -> None:
    """Increment unread_ai_count."""
    with get_engine().begin() as conn:
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(
            unread_ai_count=contacts.c.unread_ai_count + 1,
            updated_at=time.time(),
        ))


def mark_as_read(contact_id: int) -> list[str]:
    """Reset unread counts and return the unread msg_ids (for read receipts)."""
    with get_engine().begin() as conn:
        rows = conn.execute(
            select(unread_msg_ids.c.msg_id).where(unread_msg_ids.c.contact_id == contact_id)
        ).all()
        msg_ids = [r.msg_id for r in rows]
        conn.execute(sa_delete(unread_msg_ids).where(unread_msg_ids.c.contact_id == contact_id))
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(
            unread_count=0,
            unread_ai_count=0,
            has_unread_mention=0,
            updated_at=time.time(),
        ))
    return msg_ids


def unread_conversation_count() -> int:
    """Number of non-archived conversations that have unread messages — used for the
    browser-tab badge (e.g. "(3) WhatsBot-Lite"). Counts a conversation once regardless of
    how many messages are unread, mirroring the sidebar badge visibility."""
    with get_engine().connect() as conn:
        return conn.execute(
            select(func.count()).select_from(contacts).where(
                (contacts.c.is_archived == 0)
                & ((contacts.c.unread_count > 0) | (contacts.c.unread_ai_count > 0))
            )
        ).scalar() or 0


def set_mention(contact_id: int) -> None:
    """Raise the unread-mention flag (bot was @mentioned in a group). Shown as an
    "@" next to the unread badge until the operator opens the conversation."""
    with get_engine().begin() as conn:
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(
            has_unread_mention=1,
            updated_at=time.time(),
        ))


def mark_as_unread(contact_id: int) -> None:
    """Mark a contact as unread by ensuring unread_count is at least 1.

    Only touches the in-app green badge; preserves an already-higher count.
    """
    with get_engine().begin() as conn:
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(
            unread_count=case(
                (contacts.c.unread_count < 1, 1),
                else_=contacts.c.unread_count,
            ),
            updated_at=time.time(),
        ))


def mark_all_as_unread() -> int:
    """Mark every conversation as unread (green badge).

    Only rows currently at 0 are touched, so existing higher counts are kept.
    Returns the number of conversations newly marked.
    """
    with get_engine().begin() as conn:
        result = conn.execute(
            sa_update(contacts).where(contacts.c.unread_count < 1).values(
                unread_count=1,
                updated_at=time.time(),
            )
        )
    return result.rowcount or 0


def mark_all_as_read() -> int:
    """Reset unread counts for every conversation (clear all in-app badges).

    App-only: clears the tracked unread msg_ids too, but does not send WhatsApp
    read receipts. Returns the number of conversations that had unread badges.
    """
    with get_engine().begin() as conn:
        conn.execute(sa_delete(unread_msg_ids))
        result = conn.execute(
            sa_update(contacts)
            .where((contacts.c.unread_count > 0) | (contacts.c.unread_ai_count > 0)
                   | (contacts.c.has_unread_mention > 0))
            .values(unread_count=0, unread_ai_count=0, has_unread_mention=0, updated_at=time.time())
        )
    return result.rowcount or 0


def mark_user_messages_as_read(contact_id: int) -> list[str]:
    """Reset only unread_count (user messages) and return msg_ids for read receipts."""
    with get_engine().begin() as conn:
        rows = conn.execute(
            select(unread_msg_ids.c.msg_id).where(unread_msg_ids.c.contact_id == contact_id)
        ).all()
        msg_ids = [r.msg_id for r in rows]
        conn.execute(sa_delete(unread_msg_ids).where(unread_msg_ids.c.contact_id == contact_id))
        conn.execute(sa_update(contacts).where(contacts.c.id == contact_id).values(
            unread_count=0,
            updated_at=time.time(),
        ))
    return msg_ids


def get_observations(contact_id: int) -> list[str]:
    """Return all observations for a contact."""
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(observations.c.text)
            .where(observations.c.contact_id == contact_id)
            .order_by(observations.c.created_at)
        ).all()
    return [r.text for r in rows]


def set_observations(contact_id: int, observations_list: list[str]) -> None:
    """Replace all observations for a contact."""
    now = time.time()
    cleaned = [t for t in observations_list if t.strip()]
    with get_engine().begin() as conn:
        conn.execute(sa_delete(observations).where(observations.c.contact_id == contact_id))
        if cleaned:
            conn.execute(sa_insert(observations), [
                {"contact_id": contact_id, "text": t, "created_at": now} for t in cleaned
            ])


def add_observation(contact_id: int, text: str) -> None:
    """Append a single observation if it doesn't already exist."""
    with get_engine().begin() as conn:
        existing = conn.execute(
            select(observations.c.id).where(
                (observations.c.contact_id == contact_id) & (observations.c.text == text)
            )
        ).first()
        if existing:
            return
        conn.execute(sa_insert(observations).values(
            contact_id=contact_id, text=text, created_at=time.time()
        ))


def _contact_ids_matching_message(folded_q: str, archived: bool) -> dict[int, dict]:
    """Map of contact id -> ``{"snippet": str, "id": int}`` for contacts (within the
    given archived scope) that have at least one message whose content matches
    ``folded_q`` (accent/case-insensitive, like names). The match comes from the most
    recent matching message; ``id`` is its DB primary key (so the UI can scroll to it).

    Covers normal messages, private notes and transcriptions; only the purely
    internal roles (tool calls, system notices) are skipped. Revoked messages are
    kept in the DB with their content, so they remain searchable too.
    """
    if not folded_q:
        return {}
    from sqlalchemy import text as sql_text

    # Most recent first, so the first match seen per contact is the freshest one.
    sql = sql_text("""
        SELECT m.id, m.contact_id, m.content
        FROM messages m
        JOIN contacts c ON c.id = m.contact_id
        WHERE c.is_archived = :archived
          AND m.content <> ''
          AND m.role NOT IN ('tool_call', 'system_notice')
        ORDER BY m.ts DESC
    """)
    matched: dict[int, dict] = {}
    with get_engine().connect() as conn:
        for row in conn.execute(sql, {"archived": 1 if archived else 0}).mappings():
            cid = row["contact_id"]
            if cid in matched:
                continue
            content = row["content"] or ""
            if folded_q in _fold(content):
                matched[cid] = {"snippet": _match_snippet(content, folded_q), "id": row["id"]}
    return matched


def list_contacts(q: str = "", archived: bool = False) -> list[dict]:
    """List contacts with last message preview, tags, and unread counts."""
    from sqlalchemy import text as sql_text

    # Single SQL statement — easier to read than building it via Core.
    # Only standard SQL (MAX, GROUP BY, INNER JOIN, LEFT JOIN, COALESCE),
    # works in both SQLite and Postgres unchanged.
    sql = sql_text("""
        SELECT c.*,
               lm.content   AS last_msg_content,
               lm.role      AS last_msg_role,
               lm.ts        AS last_msg_ts,
               lm.media_type AS last_msg_media_type,
               lm.status    AS last_msg_status,
               lm.msg_id    AS last_msg_id,
               (SELECT COUNT(*) FROM messages WHERE contact_id = c.id) AS msg_count
        FROM contacts c
        LEFT JOIN (
            SELECT m1.contact_id, m1.content, m1.role, m1.ts, m1.media_type, m1.status, m1.msg_id
            FROM messages m1
            INNER JOIN (
                SELECT contact_id, MAX(ts) AS max_ts
                FROM messages
                WHERE role NOT IN ('transcription', 'system_notice')
                GROUP BY contact_id
            ) m2 ON m1.contact_id = m2.contact_id AND m1.ts = m2.max_ts
        ) lm ON lm.contact_id = c.id
        WHERE c.is_archived = :archived
        ORDER BY c.is_pinned DESC, COALESCE(lm.ts, c.updated_at) DESC
    """)

    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"archived": 1 if archived else 0}).mappings().all()

        # One query for every contact's tags (tag id order) instead of one per
        # contact: a 378 contact list used to cost 379 queries.
        tags_by_contact: dict[int, list[str]] = {}
        for tag_row in conn.execute(
            select(contact_tags.c.contact_id, tags.c.name)
            .join(tags, contact_tags.c.tag_id == tags.c.id)
            .order_by(tags.c.id)
        ).all():
            tags_by_contact.setdefault(tag_row.contact_id, []).append(tag_row.name)

        results = []
        for row in rows:
            contact_id = row["id"]
            tags_list = tags_by_contact.get(contact_id, [])

            last_content = ""
            lmt = row["last_msg_media_type"]
            if row["last_msg_content"] is not None:
                if lmt == "image":
                    last_content = (row["last_msg_content"] or "")[:80] or "\U0001f4f7 Imagem"
                elif lmt == "audio":
                    last_content = "\U0001f3a4 Áudio"
                else:
                    last_content = (row["last_msg_content"] or "")[:80]

            is_group = bool(row["is_group"])
            group_name = row["group_name"] or ""
            name = group_name if is_group else (row["name"] or "")

            results.append({
                "id": contact_id,
                "phone": row["phone"],
                "name": name,
                "last_message": last_content,
                "last_message_role": row["last_msg_role"] or "",
                "last_message_ts": row["last_msg_ts"] or 0,
                "last_message_status": row["last_msg_status"] or "",
                "last_message_msg_id": row["last_msg_id"] or "",
                "msg_count": row["msg_count"] or 0,
                "unread_count": row["unread_count"],
                "unread_ai_count": row["unread_ai_count"],
                "has_unread_mention": bool(row["has_unread_mention"]),
                "ai_enabled": bool(row["ai_enabled"]),
                "is_group": is_group,
                "group_name": group_name,
                "is_archived": bool(row["is_archived"]),
                "archived_by_app": bool(row["archived_by_app"]) if row["archived_by_app"] is not None else False,
                "is_pinned": bool(row["is_pinned"]) if row["is_pinned"] is not None else False,
                "can_send": bool(row["can_send"]) if row["can_send"] is not None else True,
                "tags": tags_list,
                "updated_at": row["updated_at"],
            })

    if q:
        ql = _fold(q)
        # Also match by message content (normal, private notes, transcriptions),
        # so the search bar finds a conversation by something that was said in it.
        msg_matched = _contact_ids_matching_message(ql, archived)
        filtered = []
        for c in results:
            if (ql in _fold(c["name"])
                    or ql in c["phone"]
                    or ql in _fold(c.get("group_name", ""))
                    or any(ql in _fold(t) for t in c.get("tags", []))):
                filtered.append(c)
            elif c["id"] in msg_matched:
                # Matched only by message content — show the matching excerpt so the
                # operator sees why this conversation came up, and the message id so
                # opening it can scroll straight to that message.
                c["match_snippet"] = msg_matched[c["id"]]["snippet"]
                c["match_msg_id"] = msg_matched[c["id"]]["id"]
                filtered.append(c)
        results = filtered

    return results


def get_full_contact(phone: str) -> dict | None:
    """Get full contact data for API response (contact + info + observations)."""
    variants = _br_phone_variants(phone)
    with get_engine().connect() as conn:
        row = conn.execute(
            select(contacts).where(contacts.c.phone.in_(variants))
        ).mappings().first()
        if row is None:
            return None
        contact_id = row["id"]
        obs_rows = conn.execute(
            select(observations.c.text)
            .where(observations.c.contact_id == contact_id)
            .order_by(observations.c.created_at)
        ).all()
        tag_rows = conn.execute(
            select(tags.c.name)
            .join(contact_tags, contact_tags.c.tag_id == tags.c.id)
            .where(contact_tags.c.contact_id == contact_id)
        ).all()

    data = _row_to_dict(row)
    data["info"] = {
        "name": row["name"],
        "email": row["email"],
        "profession": row["profession"],
        "company": row["company"],
        "address": row["address"],
        "observations": [r.text for r in obs_rows],
    }
    data["tags"] = [t.name for t in tag_rows]
    return data


def _row_to_dict(row) -> dict:
    """Convert a SQLAlchemy mapping row to a plain dict with Python types."""
    return {
        "id": row["id"],
        "phone": row["phone"],
        "name": row["name"],
        "email": row["email"],
        "profession": row["profession"],
        "company": row["company"],
        "address": row["address"],
        "ai_enabled": bool(row["ai_enabled"]),
        "is_group": bool(row["is_group"]),
        "group_name": row["group_name"],
        "is_archived": bool(row["is_archived"]),
        "archived_by_app": bool(row["archived_by_app"]) if row["archived_by_app"] is not None else False,
        "is_pinned": bool(row.get("is_pinned")) if hasattr(row, "get") else bool(row["is_pinned"]),
        "can_send": bool(row["can_send"]) if row["can_send"] is not None else True,
        "unread_count": row["unread_count"],
        "unread_ai_count": row["unread_ai_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
