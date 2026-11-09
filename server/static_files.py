"""StaticFiles that always revalidate, so an installed app never runs stale JS/CSS."""

from fastapi.staticfiles import StaticFiles


class NoCacheStaticFiles(StaticFiles):
    """Adds ``Cache-Control: no-cache`` ("cache, but revalidate every load").

    Plain StaticFiles sends only ETag/Last-Modified, so browsers fall back to
    heuristic caching and may reuse an old ES module — a phone with the installed
    app then keeps the previous UI after an update, or mixes a new component with
    an old module it imports. With the ETag the server answers 304 when nothing
    changed, so the cost is one cheap conditional request per asset.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
