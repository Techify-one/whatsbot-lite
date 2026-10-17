"""File manager endpoints: see what takes space and delete received/sent media.

All filesystem rules live in :mod:`server.file_manager`. The client only ever
sends opaque ids returned by the listing, never a path.
"""

import asyncio
import logging
import os

from fastapi.responses import FileResponse

from db.repositories import contact_repo, message_repo
from db.textmatch import fold, match_snippet
from server import file_manager as fm
from server.avatars import avatar_version
from server.helpers import _err, _ok

logger = logging.getLogger(__name__)


def _limit_bytes(settings) -> int:
    try:
        gb = float(settings.get("disk_limit_gb", 0.0) or 0.0)
    except (TypeError, ValueError):
        gb = 0.0
    return int(gb * 1024 ** 3) if gb > 0 else 0


def _sender_name(media_name, media_type, content: str) -> str:
    """The name the sender gave a file: the stored one, else (documents saved
    before ``media_name`` existed) the one inside ``[Documento recebido: x.pdf]``."""
    name = fm.clean_name(media_name)
    if not name and media_type == "document":
        name = fm.name_from_content(content)
    return name


def _sender_names(paths: list[str]) -> dict[str, str]:
    """``media_path`` -> the name the sender gave that file, where one is known.

    Reads ``messages.media_name`` and, for documents saved before that column
    existed, the name inside ``[Documento recebido: nome.ext]``. Files with no
    name of their own (photos, audio, video: WhatsApp does not carry one) are
    absent and keep their on-disk name.
    """
    names: dict[str, str] = {}
    for row in message_repo.media_name_rows(paths):
        path = row["media_path"]
        if path in names:
            continue
        name = _sender_name(row["media_name"], row["media_type"], row["content_head"])
        if name:
            names[path] = name
    return names


#: ``chat_type`` values of the list endpoint.
CHAT_TYPES = ("individual", "group")
#: How much of a message's text the search reads. Transcriptions can be long;
#: this keeps the single query light while still reaching well past the start.
_SEARCH_TEXT_CHARS = 1500


def build_selector(q: str = "", conversation: str = "", chat_type: str = "",
                   contact_id: int | None = None):
    """The filters that need the database, as one callable for the listing.

    ``q`` looks at the name shown for the file and at the words of the message
    that brought it (caption, audio transcription, image description; the
    placeholders the app writes around them are ignored). ``conversation`` keeps
    files whose message is in a contact or group that name/number matches, and
    ``chat_type`` keeps only private chats or only groups. ``contact_id`` is the
    exact conversation picked from the suggestions: two clients can share a name,
    so once one is picked the typed ``conversation`` text no longer matters.
    Returns ``None`` when no such filter is set, so an unfiltered listing costs
    nothing extra.

    The selector returns ``None`` to drop a file, or the extra fields to show on
    its row (``match`` = ``"name"``/``"text"`` and, for text, a ``snippet``).
    """
    q = (q or "").strip()
    conversation = (conversation or "").strip()
    chat_type = chat_type if chat_type in CHAT_TYPES else ""
    contact_id = contact_id if contact_id and contact_id > 0 else None
    if not (q or conversation or chat_type or contact_id):
        return None

    folded_q = fold(q)
    if contact_id:
        wanted = {contact_id}
    else:
        wanted = contact_repo.search_ids(conversation) if conversation else None
    by_path: dict[str, list[dict]] = {}
    for row in message_repo.media_message_rows(_SEARCH_TEXT_CHARS if q else 1):
        by_path.setdefault(row["media_path"], []).append(row)

    def select(entry: fm.Entry, db_path: str) -> dict | None:
        msgs = by_path.get(db_path, [])
        if chat_type or wanted is not None:
            # A file no message points at belongs to no conversation, so it
            # cannot satisfy a conversation or private/group filter.
            msgs = [
                m for m in msgs
                if (not chat_type or bool(m["is_group"]) == (chat_type == "group"))
                and (wanted is None or m["contact_id"] in wanted)
            ]
            if not msgs:
                return None
        if not q:
            return {}
        sender = next((n for n in (_sender_name(m["media_name"], m["media_type"], m["content"])
                                   for m in msgs) if n), "")
        shown = fm.final_name(entry.rel.rsplit("/", 1)[-1], sender)
        if folded_q in fold(shown):
            return {"match": "name"}
        for m in msgs:
            text = fm.searchable_text(m["content"])
            if text and folded_q in fold(text):
                return {"match": "text", "snippet": match_snippet(text, folded_q)}
        return None

    return select


def register_routes(app, deps):
    settings = deps.settings
    ws_manager = deps.ws_manager
    manager: fm.FileManager = deps.file_manager

    def _decorate(root: str, page: dict) -> dict:
        """Add "where did this come from" to the files of one page."""
        # Top-level folders of the received root are named after the chat's
        # phone (GOWA's layout): show the contact/group name instead.
        if root == "received" and not page.get("path"):
            dirs = [r for r in page["items"] if r["type"] == "dir"]
            wanted = [d["name"] for d in dirs] + [f"{d['name']}@g.us" for d in dirs]
            known_chats = contact_repo.display_names(wanted)
            for d in dirs:
                info = known_chats.get(d["name"]) or known_chats.get(f"{d['name']}@g.us")
                if info and info["name"]:
                    d["label"] = info["name"]
                    d["is_group"] = info["is_group"]
        files = [r for r in page["items"] if r["type"] == "file"]
        db_paths = [manager.db_path(root, r["rel"]) for r in files]
        known = message_repo.paths_with_messages(db_paths)
        for r in files:
            info = known.get(manager.db_path(root, r["rel"]))
            r["orphan"] = info is None
            if info:
                name = (info.get("group_name") if info.get("is_group") else info.get("name")) or ""
                r["origin"] = {
                    "contact_id": info["contact_id"],
                    "message_id": info["message_id"],
                    "phone": info["phone"],
                    "name": name or info["phone"],
                    "is_group": bool(info.get("is_group")),
                    "msg_ts": info["ts"],
                }
        return page

    @app.get("/api/files/usage")
    async def files_usage(refresh: bool = False):
        if refresh:
            await asyncio.to_thread(manager.invalidate)
        usage = await asyncio.to_thread(manager.usage)
        limit = _limit_bytes(settings)
        usage["limit_bytes"] = limit
        usage["limit_locked"] = bool(os.environ.get("WHATSBOT_DISK_LIMIT_GB", "").strip())
        usage["percent"] = round(usage["total_bytes"] * 100 / limit, 1) if limit else None
        return _ok(usage)

    @app.get("/api/files/conversations")
    async def files_conversations(q: str = "", limit: int = 8):
        """Contacts and groups that ``q`` names (name, group name or number), so
        the "Conversa" filter can show who each one is (photo + number) and the
        picked one is filtered by id. Read-only: nothing is created."""
        limit = max(1, min(int(limit), 20))
        rows = await asyncio.to_thread(contact_repo.suggest, q, limit)
        for r in rows:
            r["avatar_v"] = avatar_version(settings, r["phone"])
        return _ok(rows)

    @app.get("/api/files")
    async def files_list(
        root: str = "received", path: str = "", flat: bool = False,
        sort: str = "date", order: str = "desc", kind: str = "",
        orphans: bool = False, offset: int = 0, limit: int = 100,
        q: str = "", conversation: str = "", chat_type: str = "",
        contact_id: int | None = None,
        date_from: float | None = None, date_to: float | None = None,
    ):
        """``date_from``/``date_to`` are epoch seconds (the browser turns the
        picked days into its own midnights, so the time zone is the user's);
        ``date_to`` is exclusive. See :func:`build_selector` for the others."""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        def work():
            filters = dict(
                date_from=date_from, date_to=date_to,
                selector=build_selector(q, conversation, chat_type, contact_id),
            )
            if not orphans:
                page = manager.list_entries(
                    root, path=path, flat=flat, sort=sort, order=order, kind=kind,
                    offset=offset, limit=limit, name_resolver=_sender_names, **filters,
                )
                return _decorate(root, page)
            # "Orphan" = no message points at the file, which needs the message
            # lookup for every file, so walk all of them and page the result.
            kept: list[dict] = []
            off = 0
            while off is not None:
                chunk = manager.list_entries(
                    root, path=path, flat=True, sort=sort, order=order, kind=kind,
                    offset=off, limit=500, **filters,
                )
                kept.extend(r for r in _decorate(root, chunk)["items"] if r.get("orphan"))
                off = chunk["next_offset"]
            return {
                "items": kept[offset:offset + limit],
                "total": len(kept),
                "offset": offset,
                "next_offset": offset + limit if offset + limit < len(kept) else None,
                "path": path.strip("/"),
                "total_bytes": sum(r["size"] for r in kept),
            }

        try:
            page = await asyncio.to_thread(work)
        except fm.FileAccessError:
            return _err("Pasta inválida.", status=400)
        return _ok(page)

    @app.get("/api/files/{file_id}/preview")
    async def files_preview(file_id: str, download: bool = False):
        try:
            root, rel, target, is_dir = await asyncio.to_thread(manager.resolve, file_id)
        except fm.FileAccessError:
            return _err("Arquivo não encontrado.", status=404)
        if is_dir:
            return _err("Isso é uma pasta.", status=400)
        # ``None`` = not something the viewer shows (HTML/SVG are never inline, so
        # user-sent active content cannot run in the app's origin).
        shown = await asyncio.to_thread(fm.preview_mime, target)
        mime = shown or "application/octet-stream"
        inline = not download and shown is not None
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=60",
        }
        # Download under the name the sender gave it ("Teste.mp4"), not the
        # on-disk name; the id is still the only thing the client controls.
        names = await asyncio.to_thread(_sender_names, [manager.db_path(root, rel)])
        download_name = fm.final_name(target.name, names.get(manager.db_path(root, rel)))
        return FileResponse(
            str(target), media_type=mime, filename=download_name,
            content_disposition_type="inline" if inline else "attachment",
            headers=headers,
        )

    @app.post("/api/files/delete")
    async def files_delete(body: dict):
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            return _err("Selecione ao menos um item.", status=400)
        try:
            result = await asyncio.to_thread(manager.delete, ids)
        except fm.FileAccessError:
            return _err("Itens demais de uma vez. Apague em lotes menores.", status=400)

        affected = 0
        if result.removed_paths:
            affected = await asyncio.to_thread(message_repo.mark_media_removed, result.removed_paths)
        await ws_manager.broadcast("storage_changed", {
            "files": result.files, "bytes": result.bytes, "messages": affected,
        })
        return _ok({
            "files": result.files,
            "bytes": result.bytes,
            "failed": result.failed,
            "messages": affected,
        })
