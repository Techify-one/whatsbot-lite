"""Public, static PWA resources, independent of application state and credentials."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse


def register_pwa_routes(app: FastAPI, web_dir: Path) -> None:
    """Expose the worker at the origin root so its default scope covers the app."""
    headers = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        return FileResponse(
            web_dir / "sw.js",
            media_type="application/javascript",
            headers={**headers, "Service-Worker-Allowed": "/"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest() -> FileResponse:
        return FileResponse(
            web_dir / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers=headers,
        )

    @app.get("/offline.html", include_in_schema=False)
    async def offline() -> FileResponse:
        return FileResponse(web_dir / "offline.html", media_type="text/html", headers=headers)
