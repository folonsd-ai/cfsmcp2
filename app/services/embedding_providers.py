"""Embedding provider presets and URL/auth helpers.

Stdlib-only on purpose: the resolution rules (base URL -> endpoint, auth
headers) are unit-testable without FastAPI, SQLite or a running server.
The runtime settings layer sits on top of this module.

Two providers:

* ``lmstudio`` — the default, unchanged since 0.1: a local LM Studio server on
  ``http://127.0.0.1:1234``, no API key, no ``Authorization`` header;
* ``routerai`` — a Russian OpenAI-compatible router (billing in roubles) for
  machines that cannot run local embeddings at all.

Both speak the OpenAI embeddings API, so one client covers them.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

LMSTUDIO = "lmstudio"
ROUTERAI = "routerai"

DEFAULT_PROVIDER = LMSTUDIO

PROVIDERS: dict[str, dict] = {
    LMSTUDIO: {
        "id": LMSTUDIO,
        "label": "LM Studio",
        # Empty: the effective URL comes from lm_studio_url, unchanged since 0.1.
        "base_url": "",
        "requires_key": False,
        "default_model": "text-embedding-multilingual-e5-small",
        # "" = LM Studio native /api/v0/models first, then /v1/models.
        "models_path": "",
        "docs_url": "https://lmstudio.ai/",
        # LM Studio answers locally and fast: always ask the live server.
        "suggested_models": [],
        # Local server: no account, no balance, no top-up page.
        "balance_path": "",
        "topup_url": "",
    },
    ROUTERAI: {
        "id": ROUTERAI,
        "label": "RouterAI",
        "base_url": "https://routerai.ru/api/v1",
        "requires_key": True,
        "default_model": "openai/text-embedding-3-small",
        "models_path": "models",
        "docs_url": "https://routerai.ru/models/openai/text-embedding-3-small",
        # Shown instantly; the full catalog is one "Обновить модели" click away.
        "suggested_models": [
            "intfloat/multilingual-e5-large",
            "intfloat/e5-large-v2",
            "openai/text-embedding-3-small",
            "google/gemini-embedding-001",
            "mistralai/mistral-embed-2312",
        ],
        # GET /v1/credits -> {"data": {"credits": 123.45}} (roubles)
        "balance_path": "credits",
        "topup_url": "https://routerai.ru/settings/billing",
    },
}

ALLOWED_PROVIDERS = frozenset(PROVIDERS)


def normalize_provider(name: str | None) -> str:
    """Unknown or removed providers (e.g. the old ``openrouter``) fall back to LM Studio."""
    p = (name or "").strip().lower()
    return p if p in ALLOWED_PROVIDERS else DEFAULT_PROVIDER


def provider_preset(name: str | None) -> dict:
    return dict(PROVIDERS[normalize_provider(name)])


def providers_public() -> list[dict]:
    """Presets shaped for the settings API / UI (no secrets involved)."""
    out: list[dict] = []
    for key, preset in PROVIDERS.items():
        out.append(
            {
                "id": key,
                "label": preset["label"],
                "base_url": preset["base_url"],
                "requires_key": bool(preset["requires_key"]),
                "default_model": preset["default_model"],
                "docs_url": preset["docs_url"],
                "suggested_models": list(preset.get("suggested_models") or []),
            }
        )
    return out


def suggested_models(provider: str | None) -> list[str]:
    return list(provider_preset(provider).get("suggested_models") or [])


def normalize_base_url(url: str | None) -> str:
    """Trim and drop the trailing slash; empty stays empty."""
    return (url or "").strip().rstrip("/")


def is_http_url(url: str | None) -> bool:
    s = (url or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def endpoint_for(base_url: str, path: str = "embeddings") -> str:
    """Absolute URL for an OpenAI-compatible call under ``base_url``.

    The base may or may not carry the ``/v1`` suffix, which covers both
    providers without special cases:

        http://127.0.0.1:1234      -> http://127.0.0.1:1234/v1/embeddings
        https://routerai.ru/api/v1 -> https://routerai.ru/api/v1/embeddings
    """
    base = normalize_base_url(base_url)
    if not base:
        raise ValueError("embedding base URL is empty")
    parts = urlsplit(base)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"embedding base URL must be absolute: {base!r}")
    tail = path.strip("/")
    if parts.path.rstrip("/").endswith("/v1"):
        return f"{base}/{tail}"
    return f"{base}/v1/{tail}"


def auth_headers(api_key: str | None) -> dict[str, str]:
    key = (api_key or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def auth_tag(api_key: str | None) -> str:
    """Short non-reversible tag so caches never mix two keys on one base URL."""
    key = (api_key or "").strip()
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def mask_key(api_key: str | None) -> str:
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "\u2022" * len(key)
    return f"{key[:4]}\u2026{key[-4:]}"


def models_list_path(provider: str | None) -> str:
    return provider_preset(provider).get("models_path") or "models"


def is_local(provider: str | None) -> bool:
    return normalize_provider(provider) == LMSTUDIO
