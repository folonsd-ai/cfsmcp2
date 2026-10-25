from __future__ import annotations

import time

import httpx

from app.services import embedding_providers as ep
from app.services.runtime_settings import (
    get_default_embedding_model,
    get_embedding_api_key_for,
    get_embedding_base_url_for,
    get_embedding_provider,
)


def _ids_from_payload(payload: object) -> list[dict]:
    """Tolerate the shapes seen in the wild: {"data": [...]}, {"models": [...]}, [...]."""
    rows: list[dict] = []
    if isinstance(payload, list):
        seq = payload
    elif isinstance(payload, dict):
        seq = None
        for key in ("data", "models", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                seq = value
                break
        if seq is None:
            return rows
    else:
        return rows
    for item in seq:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
        else:
            mid = item
        mid = str(mid or "").strip()
        if not mid:
            continue
        rows.append(
            {
                "id": mid,
                "state": str(item.get("state") or "unknown") if isinstance(item, dict) else "unknown",
                "publisher": str(item.get("publisher") or item.get("owned_by") or "")
                if isinstance(item, dict)
                else "",
                "quantization": str(item.get("quantization") or "") if isinstance(item, dict) else "",
                "max_context_length": item.get("max_context_length")
                if isinstance(item, dict)
                else None,
            }
        )
    return rows


# Hosted catalogs are big and slow (RouterAI answers ~600 models in ~48 s),
# so a live listing is cached and only fetched on an explicit refresh.
_MODELS_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_MODELS_CACHE_TTL_SEC = 600.0


def _cached_models(key: tuple) -> list[dict] | None:
    hit = _MODELS_CACHE.get(key)
    if not hit:
        return None
    ts, rows = hit
    if time.time() - ts > _MODELS_CACHE_TTL_SEC:
        _MODELS_CACHE.pop(key, None)
        return None
    return rows


def _suggested(provider: str) -> list[dict]:
    return [
        {
            "id": mid,
            "state": "suggested",
            "publisher": "",
            "quantization": "",
            "max_context_length": None,
        }
        for mid in ep.suggested_models(provider)
    ]


def list_embedding_models(
    base_url: str | None = None,
    timeout: float = 8.0,
    live: bool = True,
    *,
    provider: str | None = None,
    api_key: str | None = None,
) -> dict:
    """List embedding models for the configured provider.

    LM Studio (default) keeps the native ``/api/v0/models`` probe with the
    ``/v1/models`` heuristic as fallback and is always queried live: it is
    local and fast.

    Hosted providers return a curated short list unless ``live`` is set, so
    opening the UI never blocks on a slow cloud catalog.
    """
    provider = ep.normalize_provider(provider) if provider is not None else get_embedding_provider()
    url = ep.normalize_base_url(
        base_url if base_url is not None else get_embedding_base_url_for(provider)
    )
    api_key = (
        get_embedding_api_key_for(provider) if api_key is None else (api_key or "").strip()
    )
    headers = ep.auth_headers(api_key)
    preset = ep.provider_preset(provider)
    models: list[dict] = []
    error: str | None = None
    source = "none"

    if preset["requires_key"] and not api_key:
        return {
            "ok": False,
            "lm_studio_url": url,
            "provider": provider,
            "provider_label": preset["label"],
            "source": source,
            "error": f"API key required for {preset['label']}",
            "models": models,
        }

    hosted = provider != ep.LMSTUDIO
    cache_key = (provider, url, ep.auth_tag(api_key))
    # LM Studio state changes as the user loads models: never serve it stale.
    cached = _cached_models(cache_key) if hosted else None
    if cached is not None:
        # A real catalog beats a curated guess even when ``live`` is off.
        return {
            "ok": True,
            "lm_studio_url": url,
            "provider": provider,
            "provider_label": preset["label"],
            "source": "cache",
            "error": None,
            "models": cached,
        }
    if hosted and not live:
        return {
            "ok": True,
            "lm_studio_url": url,
            "provider": provider,
            "provider_label": preset["label"],
            "source": "preset",
            "error": None,
            "models": _suggested(provider),
        }
    if hosted:
        # RouterAI lists ~600 models and answers in ~48 s: give it room.
        timeout = max(timeout, 60.0)

    try:
        with httpx.Client(timeout=timeout, headers=headers) as client:
            native_done = False
            if provider == ep.LMSTUDIO:
                # Prefer the native API: it flags type=embeddings explicitly.
                r = client.get(f"{url}/api/v0/models")
                if r.status_code == 200:
                    native_done = True
                    source = "api/v0/models"
                    for m in r.json().get("data") or []:
                        if (m.get("type") or "").lower() != "embeddings":
                            continue
                        models.append(
                            {
                                "id": m["id"],
                                "state": m.get("state") or "unknown",
                                "publisher": m.get("publisher") or "",
                                "quantization": m.get("quantization") or "",
                                "max_context_length": m.get("max_context_length"),
                            }
                        )
            if not native_done:
                path = ep.models_list_path(provider)
                target = ep.endpoint_for(url, path)
                r = client.get(target)
                r.raise_for_status()
                source = f"v1/{path}"
                # RouterAI's ``/v1/models`` also carries chat models (~600 of
                # them), so keep only ids that look like embeddings. An id the
                # filter misses can still be typed by hand in the dialog.
                for row in _ids_from_payload(r.json()):
                    if not _looks_like_embedding(row["id"]):
                        continue
                    models.append(row)
    except Exception as exc:
        error = str(exc)

    models.sort(key=lambda x: (0 if x.get("state") == "loaded" else 1, x["id"].lower()))
    if not error and models and hosted:
        _MODELS_CACHE[cache_key] = (time.time(), models)
    elif error and hosted:
        # Never leave the UI empty because the cloud catalog was slow.
        models = models or _suggested(provider)
    return {
        "ok": error is None,
        "lm_studio_url": url,
        "provider": provider,
        "provider_label": preset["label"],
        "source": source,
        "error": error,
        "models": models,
    }


# Name fragments of real embedding families. "embed" covers embed/embedding;
# the rest catch families that never spell it out (intfloat/e5-large-v2).
_EMBEDDING_NAME_KEYS = ("embed", "e5-", "bge", "nomic", "gte-", "jina", "minilm", "mxbai")


def _looks_like_embedding(model_id: str) -> bool:
    s = model_id.lower()
    return any(k in s for k in _EMBEDDING_NAME_KEYS)


def ping(
    base_url: str | None = None,
    timeout: float = 5.0,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    provider = ep.normalize_provider(provider) if provider is not None else get_embedding_provider()
    url = ep.normalize_base_url(
        base_url if base_url is not None else get_embedding_base_url_for(provider)
    )
    api_key = (
        get_embedding_api_key_for(provider) if api_key is None else (api_key or "").strip()
    )
    preset = ep.provider_preset(provider)
    if preset["requires_key"] and not api_key:
        return {
            "ok": False,
            "lm_studio_url": url,
            "provider": provider,
            "error": f"API key required for {preset['label']}",
        }
    model = (model or "").strip() or get_default_embedding_model()
    if preset["requires_key"] or provider != ep.LMSTUDIO:
        # Cheapest honest check: one tiny embedding round-trip validates URL,
        # key and model id at once (a model catalog can be slow or huge).
        from app.services.embeddings import EmbeddingClient

        try:
            vectors = EmbeddingClient(base_url=url, api_key=api_key).embed(
                ["ping"], model, for_query=False
            )
            return {
                "ok": True,
                "lm_studio_url": url,
                "provider": provider,
                "provider_label": preset["label"],
                "model": model,
                "dims": len(vectors[0]) if vectors else 0,
            }
        except Exception as exc:
            return {
                "ok": False,
                "lm_studio_url": url,
                "provider": provider,
                "provider_label": preset["label"],
                "model": model,
                "error": str(exc)[:300],
            }
    try:
        with httpx.Client(timeout=timeout, headers=ep.auth_headers(api_key)) as client:
            r = client.get(ep.endpoint_for(url, "models"))
            r.raise_for_status()
            return {
                "ok": True,
                "lm_studio_url": url,
                "provider": provider,
                "status_code": r.status_code,
            }
    except Exception as exc:
        return {"ok": False, "lm_studio_url": url, "provider": provider, "error": str(exc)}
