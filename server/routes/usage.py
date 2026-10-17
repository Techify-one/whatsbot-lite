"""Usage / cost tracking endpoints."""

import asyncio
import logging
import time

import httpx

from config.settings import LLM_API_BASE_URL
from db.repositories import usage_repo, contact_repo
from server.helpers import _ok
from server.routes.config import MODELS_PATHS, get_models_cache

logger = logging.getLogger(__name__)


def _get_model_pricing_details(model_id: str, api_key: str = "") -> dict:
    """Return the provider's complete pricing object for a model."""
    _models_cache = get_models_cache()
    if not _models_cache["data"]:
        try:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            raw = None
            for i, path in enumerate(MODELS_PATHS):
                try:
                    resp = httpx.get(f"{LLM_API_BASE_URL}{path}", headers=headers, timeout=15)
                    resp.raise_for_status()
                    raw = resp.json()
                    break
                except Exception:
                    if i == len(MODELS_PATHS) - 1:
                        raise
            models = []
            for m in raw.get("data", []):
                arch = m.get("architecture", {})
                models.append({
                    "id": m.get("id", ""),
                    "name": m.get("name", ""),
                    "input_modalities": arch.get("input_modalities", ["text"]),
                    "pricing": m.get("pricing", {}),
                })
            models.sort(key=lambda x: x["name"].lower())
            _models_cache["data"] = models
            _models_cache["fetched_at"] = time.time()
            logger.info("Models cache populated for pricing (%d models)", len(models))
        except Exception as e:
            logger.warning("Failed to fetch models for pricing: %s", e)
            return {}
    for m in _models_cache["data"]:
        if m["id"] == model_id:
            return dict(m.get("pricing", {}))
    return {}


def _get_model_pricing(model_id: str, api_key: str = "") -> tuple[float, float]:
    """Return (prompt_price_per_token, completion_price_per_token) from cache."""
    p = _get_model_pricing_details(model_id, api_key)
    if p:
        return float(p.get("prompt", "0") or "0"), float(p.get("completion", "0") or "0")
    return 0.0, 0.0


def _parse_period(period: str | None, start: float | None, end: float | None) -> tuple[float | None, float | None]:
    """Convert period shorthand or explicit timestamps to (start_ts, end_ts)."""
    if start is not None or end is not None:
        return start, end
    if not period:
        return None, None
    now = time.time()
    mapping = {"24h": 86400, "3d": 259200, "7d": 604800, "30d": 2592000}
    seconds = mapping.get(period)
    if seconds:
        return now - seconds, now
    return None, None


def register_routes(app, deps):
    agent_handler = deps.agent_handler
    settings = deps.settings

    # Wire up pricing function — closure captures settings to forward the API key
    # to the upstream proxy (Techify / OpenRouter-compatible), which requires it.
    def pricing_fn(model_id: str) -> tuple[float, float]:
        return _get_model_pricing(model_id, settings.get("openrouter_api_key", ""))

    def pricing_details_fn(model_id: str) -> dict:
        return _get_model_pricing_details(model_id, settings.get("openrouter_api_key", ""))

    agent_handler.pricing_fn = pricing_fn
    agent_handler.pricing_details_fn = pricing_details_fn

    @app.get("/api/usage/summary")
    async def usage_summary_endpoint(period: str | None = None, start: float | None = None, end: float | None = None):
        start_ts, end_ts = _parse_period(period, start, end)
        totals = await asyncio.to_thread(usage_repo.global_summary, start_ts, end_ts)
        totals["period_start"] = start_ts
        totals["period_end"] = end_ts
        return _ok(totals)

    @app.get("/api/usage/by-contact")
    async def usage_by_contact_endpoint(period: str | None = None, start: float | None = None, end: float | None = None):
        start_ts, end_ts = _parse_period(period, start, end)
        rows = await asyncio.to_thread(usage_repo.by_contact, start_ts, end_ts)
        return _ok(rows)

    @app.get("/api/usage/contact/{phone}")
    async def usage_contact_detail(phone: str, period: str | None = None, start: float | None = None, end: float | None = None):
        start_ts, end_ts = _parse_period(period, start, end)
        contact = await asyncio.to_thread(contact_repo.get_by_phone, phone)
        if contact is None:
            return _ok([])
        filtered = await asyncio.to_thread(usage_repo.detail, contact["id"], start_ts, end_ts)
        return _ok(filtered)
