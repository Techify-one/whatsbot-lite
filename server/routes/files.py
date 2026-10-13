"""File manager endpoints: see what takes space and delete received/sent media.

All filesystem rules live in :mod:`server.file_manager`. The client only ever
sends opaque ids returned by the listing, never a path.
"""

import asyncio
import logging
import mimetypes
import os

from fastapi.responses import FileResponse

from db.repositories import contact_repo, message_repo
from server import file_manager as fm
from server.helpers import _err, _ok

logger = logging.getLogger(__name__)

# Types a browser can show inline; everything else is offered as a download.
_INLINE_PREFIXES = ("image/", "audio/", "video/")
_INLINE_EXACT = {"application/pdf", "text/plain"}


def _limit_bytes(settings) -> int:
    try:
        gb = float(settings.get("disk_limit_gb", 0.0) or 0.0)
    except (TypeError, ValueError):
        gb = 0.0
    return int(gb * 1024 ** 3) if gb > 0 else 0


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

    @app.get("/api/files")
    async def files_list(
        root: str = "received", path: str = "", flat: bool = False,
        sort: str = "date", order: str = "desc", kind: str = "",
        orphans: bool = False, offset: int = 0, limit: int = 100,
    ):
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        def work():
            if not orphans:
                page = manager.list_entries(
                    root, path=path, flat=flat, sort=sort, order=order, kind=kind,
                    offset=offset, limit=limit,
                )
                return _decorate(root, page)
            # "Orphan" = no message points at the file, which needs the message
            # lookup for every file, so walk all of them and page the result.
            kept: list[dict] = []
            off = 0
            while off is not None:
                chunk = manager.list_entries(
                    root, path=path, flat=True, sort=sort, order=order, kind=kind,
                    offset=off, limit=500,
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
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        inline = not download and (mime.startswith(_INLINE_PREFIXES) or mime in _INLINE_EXACT)
        # Never let the browser render user-sent HTML/SVG in the app's origin.
        if mime in ("text/html", "image/svg+xml", "application/xhtml+xml"):
            inline = False
            mime = "application/octet-stream"
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=60",
        }
        return FileResponse(
            str(target), media_type=mime, filename=target.name,
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
