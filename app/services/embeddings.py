from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

import httpx

from app.services.runtime_settings import get_lm_studio_url
from app.services.usage_stats import usage_stats

log = logging.getLogger("cfsmcp2.embeddings")

PREFIX_MODELS = {
    "e5-large-instruct": ("query: ", "passage: "),
    "multilingual-e5-large-instruct": ("query: ", "passage: "),
}

# Fail-fast to LM Studio: short connect, longer read for large batches.
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 120.0
DEFAULT_WRITE_TIMEOUT = 30.0
DEFAULT_POOL_TIMEOUT = 5.0
# Attempts including the first; backoff sleeps between retries.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SEC = (0.5, 1.5, 3.0)
_RETRY_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Process-wide HTTP client (connection pooling) + LRU for query vectors.
_QUERY_CACHE_MAX = 256
_client_lock = threading.Lock()
_shared_client: httpx.Client | None = None
_shared_client_key: tuple | None = None
_query_cache: OrderedDict[tuple, list[float]] = OrderedDict()
_query_cache_lock = threading.Lock()
_dims_cache: dict[tuple, int] = {}
_dims_lock = threading.Lock()


class LmStudioUnavailable(RuntimeError):
    """LM Studio unreachable / overloaded after retries — safe to surface as index_error."""


def _prefixes(model: str) -> tuple[str, str]:
    for key, pref in PREFIX_MODELS.items():
        if key in model:
            return pref
    return ("", "")


def _is_transient(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRY_HTTP_STATUS
    return False


def _http_timeout(
    *,
    connect: float = DEFAULT_CONNECT_TIMEOUT,
    read: float = DEFAULT_READ_TIMEOUT,
    write: float = DEFAULT_WRITE_TIMEOUT,
    pool: float = DEFAULT_POOL_TIMEOUT,
) -> httpx.Timeout:
    return httpx.Timeout(connect=connect, read=read, write=write, pool=pool)


def _timeout_key(timeout: httpx.Timeout) -> tuple:
    return (
        float(timeout.connect),
        float(timeout.read),
        float(timeout.write),
        float(timeout.pool),
    )


def _get_shared_client(timeout: httpx.Timeout) -> httpx.Client:
    """Reuse one httpx.Client per process for TCP/TLS pooling to LM Studio."""
    global _shared_client, _shared_client_key
    key = _timeout_key(timeout)
    with _client_lock:
        if _shared_client is None or _shared_client_key != key:
            old = _shared_client
            _shared_client = httpx.Client(
                timeout=timeout,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
            _shared_client_key = key
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
        return _shared_client


def close_shared_http_client() -> None:
    global _shared_client, _shared_client_key
    with _client_lock:
        if _shared_client is not None:
            try:
                _shared_client.close()
            except Exception:
                pass
            _shared_client = None
            _shared_client_key = None


def clear_query_embedding_cache() -> None:
    with _query_cache_lock:
        _query_cache.clear()


def _query_cache_get(key: tuple) -> list[float] | None:
    with _query_cache_lock:
        vec = _query_cache.get(key)
        if vec is None:
            return None
        _query_cache.move_to_end(key)
        return list(vec)


def _query_cache_put(key: tuple, vec: list[float]) -> None:
    with _query_cache_lock:
        _query_cache[key] = list(vec)
        _query_cache.move_to_end(key)
        while len(_query_cache) > _QUERY_CACHE_MAX:
            _query_cache.popitem(last=False)


class EmbeddingClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float | httpx.Timeout | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_sec: tuple[float, ...] = DEFAULT_BACKOFF_SEC,
    ):
        self.base_url = (base_url or get_lm_studio_url()).rstrip("/")
        if isinstance(timeout, httpx.Timeout):
            self.timeout = timeout
        elif timeout is not None:
            # Legacy single float → use as read; keep short connect for fail-fast.
            self.timeout = _http_timeout(connect=connect_timeout, read=float(timeout))
        else:
            self.timeout = _http_timeout(connect=connect_timeout, read=read_timeout)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_sec = backoff_sec

    def _embed_remote(self, payload_input: list[str], model: str) -> list[list[float]]:
        url = f"{self.base_url}/v1/embeddings"
        client = _get_shared_client(self.timeout)
        last_exc: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = client.post(url, json={"model": model, "input": payload_input})
                resp.raise_for_status()
                data = resp.json()["data"]
                data.sort(key=lambda x: x["index"])
                return [row["embedding"] for row in data]
            except Exception as exc:
                last_exc = exc
                if not _is_transient(exc) or attempt >= self.max_attempts:
                    break
                delay = self.backoff_sec[min(attempt - 1, len(self.backoff_sec) - 1)]
                log.warning(
                    "LM Studio embed attempt %s/%s failed (%s); retry in %.1fs",
                    attempt,
                    self.max_attempts,
                    exc,
                    delay,
                )
                time.sleep(delay)
        assert last_exc is not None
        if _is_transient(last_exc):
            raise LmStudioUnavailable(
                f"LM Studio unavailable at {self.base_url}: {last_exc}"
            ) from last_exc
        raise last_exc

    def embed(self, texts: list[str], model: str, *, for_query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        from app.core.config import settings as app_settings

        q_pref, p_pref = _prefixes(model)
        pref = q_pref if for_query else p_pref
        payload_input = [pref + t for t in texts] if pref else list(texts)
        max_n = max(1, int(app_settings.embedding_max_texts_per_request))

        t0 = time.perf_counter()
        ok = True
        cache_hits = 0
        detail = f"batch={len(texts)} model={model}"
        try:
            # LRU only for query embeddings (search path); indexing batches stay uncached.
            if for_query:
                results: list[list[float] | None] = [None] * len(texts)
                miss_idx: list[int] = []
                miss_payload: list[str] = []
                for i, text in enumerate(texts):
                    key = (self.base_url, model, True, text)
                    cached = _query_cache_get(key)
                    if cached is not None:
                        results[i] = cached
                        cache_hits += 1
                    else:
                        miss_idx.append(i)
                        miss_payload.append(payload_input[i])
                if miss_payload:
                    remote = self._embed_remote_chunked(miss_payload, model, max_n)
                    for j, vec in enumerate(remote):
                        i = miss_idx[j]
                        results[i] = vec
                        _query_cache_put((self.base_url, model, True, texts[i]), vec)
                detail = f"batch={len(texts)} model={model} cache_hits={cache_hits}"
                return [v if v is not None else [] for v in results]

            return self._embed_remote_chunked(payload_input, model, max_n)
        except Exception as exc:
            ok = False
            detail = str(exc)[:200]
            raise
        finally:
            ms = (time.perf_counter() - t0) * 1000
            if not ok:
                usage_stats.record(
                    kind="embed",
                    name="embeddings",
                    ok=False,
                    duration_ms=ms,
                    detail=detail,
                    tier="error",
                )
            else:
                if for_query:
                    usage_stats.note_index_progress(
                        role="query",
                        model=model,
                        passages=len(texts),
                        batches=1,
                        cache_hits=cache_hits,
                        duration_ms=ms,
                        ok=True,
                    )

    def _embed_remote_chunked(
        self, payload_input: list[str], model: str, max_n: int
    ) -> list[list[float]]:
        """Split large batches so chunk-mode expansions do not overwhelm LM Studio."""
        if len(payload_input) <= max_n:
            return self._embed_remote(payload_input, model)
        out: list[list[float]] = []
        total = len(payload_input)
        for i in range(0, total, max_n):
            part = payload_input[i : i + max_n]
            log.debug(
                "embed sub-batch %s-%s / %s (max_per_req=%s)",
                i + 1,
                min(i + max_n, total),
                total,
                max_n,
            )
            out.extend(self._embed_remote(part, model))
        return out

    def dims(self, model: str) -> int:
        key = (self.base_url, model)
        with _dims_lock:
            hit = _dims_cache.get(key)
        if hit:
            return hit
        vecs = self.embed(["ping"], model)
        d = len(vecs[0])
        with _dims_lock:
            _dims_cache[key] = d
        return d
