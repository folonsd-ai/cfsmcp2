"""Account balance of a paid embedding provider (RouterAI).

The badge in the header must never break indexing, so every failure is turned
into ``ok: false`` with a readable ``error`` and the UI simply hides the badge.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.core.config import settings
from app.services import embedding_providers as ep
from app.services.runtime_settings import (
    get_embedding_api_key,
    get_embedding_base_url,
    get_embedding_provider,
)

log = logging.getLogger("cfsmcp2.balance")

CACHE_TTL_SEC = 60.0
_CACHE: dict[tuple, tuple[float, dict]] = {}


def clear_balance_cache() -> None:
    _CACHE.clear()


def _extract_amount(payload: object) -> float | None:
    """``GET /v1/credits`` answers ``{"data": {"credits": 123.45}}``."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    for key in ("credits", "balance", "amount"):
        if key in data:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                return None
    return None


def get_balance(*, live: bool = False, timeout: float = 8.0) -> dict:
    provider = get_embedding_provider()
    preset = ep.provider_preset(provider)
    result: dict = {
        "ok": False,
        "provider": provider,
        "provider_label": preset["label"],
        "balance": None,
        "currency": "RUB",
        "low": False,
        "critical": False,
        "low_threshold": float(settings.balance_low_rub),
        "critical_threshold": float(settings.balance_critical_rub),
        "topup_url": preset.get("topup_url") or "",
        "error": None,
        "cached": False,
        "checked_at": int(time.time()),
    }

    path = preset.get("balance_path") or ""
    if not path:
        # Local provider: there is no balance to watch, and the UI hides the badge.
        result["ok"] = True
        return result

    api_key = get_embedding_api_key()
    if not api_key:
        result["error"] = f"API key required for {preset['label']}"
        return result

    url = ep.normalize_base_url(get_embedding_base_url())
    cache_key = (provider, url, ep.auth_tag(api_key))
    if not live:
        hit = _CACHE.get(cache_key)
        if hit and time.time() - hit[0] <= CACHE_TTL_SEC:
            cached = dict(hit[1])
            cached["cached"] = True
            return cached

    try:
        with httpx.Client(timeout=timeout, headers=ep.auth_headers(api_key)) as client:
            resp = client.get(ep.endpoint_for(url, path))
            resp.raise_for_status()
            amount = _extract_amount(resp.json())
    except Exception as exc:  # network, 401, bad JSON — hide the badge, keep indexing
        result["error"] = str(exc)[:200]
        log.debug("balance check failed for %s: %s", provider, exc)
        return result

    if amount is None:
        result["error"] = "unexpected balance payload"
        return result

    result["ok"] = True
    result["balance"] = amount
    result["low"] = amount < result["low_threshold"]
    result["critical"] = amount < result["critical_threshold"]
    _CACHE[cache_key] = (time.time(), dict(result))
    return result
