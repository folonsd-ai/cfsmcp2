from __future__ import annotations

import httpx

from app.services.runtime_settings import get_lm_studio_url


def list_embedding_models(base_url: str | None = None, timeout: float = 8.0) -> dict:
    """Fetch embedding models from LM Studio native API, fallback to /v1/models heuristic."""
    url = (base_url or get_lm_studio_url()).rstrip("/")
    models: list[dict] = []
    error: str | None = None
    source = "none"

    try:
        with httpx.Client(timeout=timeout) as client:
            # Prefer native API: has type=embeddings
            r = client.get(f"{url}/api/v0/models")
            if r.status_code == 200:
                source = "api/v0/models"
                for m in r.json().get("data") or []:
                    if (m.get("type") or "").lower() == "embeddings":
                        models.append(
                            {
                                "id": m["id"],
                                "state": m.get("state") or "unknown",
                                "publisher": m.get("publisher") or "",
                                "quantization": m.get("quantization") or "",
                                "max_context_length": m.get("max_context_length"),
                            }
                        )
            else:
                r2 = client.get(f"{url}/v1/models")
                r2.raise_for_status()
                source = "v1/models"
                for m in r2.json().get("data") or []:
                    mid = m.get("id") or ""
                    if _looks_like_embedding(mid):
                        models.append(
                            {
                                "id": mid,
                                "state": "unknown",
                                "publisher": m.get("owned_by") or "",
                                "quantization": "",
                                "max_context_length": None,
                            }
                        )
    except Exception as exc:
        error = str(exc)

    models.sort(key=lambda x: (0 if x.get("state") == "loaded" else 1, x["id"].lower()))
    return {
        "ok": error is None,
        "lm_studio_url": url,
        "source": source,
        "error": error,
        "models": models,
    }


def _looks_like_embedding(model_id: str) -> bool:
    s = model_id.lower()
    keys = ("embed", "e5-", "bge-", "nomic-embed", "gte-", "jina-embed")
    return any(k in s for k in keys)


def ping(base_url: str | None = None, timeout: float = 5.0) -> dict:
    url = (base_url or get_lm_studio_url()).rstrip("/")
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{url}/v1/models")
            r.raise_for_status()
            return {"ok": True, "lm_studio_url": url, "status_code": r.status_code}
    except Exception as exc:
        return {"ok": False, "lm_studio_url": url, "error": str(exc)}
