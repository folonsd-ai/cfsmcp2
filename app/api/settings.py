from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.database import connect
from app.core.version import APP_VERSION
from app.schemas.settings import (
    DbInfoOut,
    McpToolsResponse,
    ModelsResponse,
    SettingsOut,
    SettingsPatch,
    VacuumResponse,
)
from app.services import lm_studio, runtime_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


@router.get("")
def get_settings() -> SettingsOut:
    data = runtime_settings.get_all()
    return SettingsOut(**data)


@router.patch("")
def patch_settings(body: SettingsPatch) -> SettingsOut:
    from app.services.bsl_embed import ALLOWED_BSL_EMBED_MODES

    if body.lm_studio_url is not None:
        url = body.lm_studio_url.strip().rstrip("/")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(400, "lm_studio_url must start with http:// or https://")
    if body.embedding_workers is not None:
        if body.embedding_workers not in runtime_settings.ALLOWED_EMBEDDING_WORKERS:
            raise HTTPException(
                400,
                "embedding_workers must be one of: "
                + ", ".join(str(x) for x in sorted(runtime_settings.ALLOWED_EMBEDDING_WORKERS)),
            )
    if body.bsl_embed_mode is not None:
        raw = body.bsl_embed_mode.strip().lower()
        if raw not in ALLOWED_BSL_EMBED_MODES:
            raise HTTPException(
                400,
                "bsl_embed_mode must be one of: " + ", ".join(sorted(ALLOWED_BSL_EMBED_MODES)),
            )
        body.bsl_embed_mode = raw
    if body.ui_poll_interval_sec is not None:
        if body.ui_poll_interval_sec not in runtime_settings.ALLOWED_UI_POLL_INTERVAL_SEC:
            raise HTTPException(
                400,
                "ui_poll_interval_sec must be one of: "
                + ", ".join(str(x) for x in sorted(runtime_settings.ALLOWED_UI_POLL_INTERVAL_SEC)),
            )
    if body.stats_window_sec is not None:
        if body.stats_window_sec not in runtime_settings.ALLOWED_STATS_WINDOW_SEC:
            raise HTTPException(
                400,
                "stats_window_sec must be one of: "
                + ", ".join(str(x) for x in sorted(runtime_settings.ALLOWED_STATS_WINDOW_SEC)),
            )
    if body.log_level is not None:
        lvl = body.log_level.strip().lower()
        if lvl not in runtime_settings.ALLOWED_LOG_LEVELS:
            raise HTTPException(
                400,
                "log_level must be one of: "
                + ", ".join(sorted(runtime_settings.ALLOWED_LOG_LEVELS)),
            )
        body.log_level = lvl
    if body.slow_request_ms is not None:
        from app.services.app_log import MAX_SLOW_MS, MIN_SLOW_MS

        if not (MIN_SLOW_MS <= int(body.slow_request_ms) <= MAX_SLOW_MS):
            raise HTTPException(
                400, f"slow_request_ms must be between {MIN_SLOW_MS} and {MAX_SLOW_MS}"
            )
    if body.app_log_retain_days is not None:
        from app.services.app_log import MAX_RETAIN_DAYS, MIN_RETAIN_DAYS

        if not (MIN_RETAIN_DAYS <= int(body.app_log_retain_days) <= MAX_RETAIN_DAYS):
            raise HTTPException(
                400,
                f"app_log_retain_days must be between {MIN_RETAIN_DAYS} and {MAX_RETAIN_DAYS}",
            )
    if body.app_log_max_rows is not None:
        from app.services.app_log import MAX_MAX_ROWS, MIN_MAX_ROWS

        if not (MIN_MAX_ROWS <= int(body.app_log_max_rows) <= MAX_MAX_ROWS):
            raise HTTPException(
                400, f"app_log_max_rows must be between {MIN_MAX_ROWS} and {MAX_MAX_ROWS}"
            )
    data = runtime_settings.update(
        lm_studio_url=body.lm_studio_url,
        default_embedding_model=body.default_embedding_model,
        embedding_workers=body.embedding_workers,
        bsl_embed_mode=body.bsl_embed_mode,
        ui_poll_interval_sec=body.ui_poll_interval_sec,
        ui_show_objects_col=body.ui_show_objects_col,
        ui_show_model_col=body.ui_show_model_col,
        stats_window_sec=body.stats_window_sec,
        log_level=body.log_level,
        experimental_call_chain=body.experimental_call_chain,
        app_log_enabled=body.app_log_enabled,
        slow_request_ms=body.slow_request_ms,
        app_log_retain_days=body.app_log_retain_days,
        app_log_max_rows=body.app_log_max_rows,
        bsl_passage_max_chars=body.bsl_passage_max_chars,
        bsl_chunk_size=body.bsl_chunk_size,
        bsl_chunk_overlap=body.bsl_chunk_overlap,
        bsl_min_body_chars=body.bsl_min_body_chars,
        bsl_max_chunks=body.bsl_max_chunks,
    )
    return SettingsOut(**data)


@router.post("/bsl-embed-limits/reset")
def reset_bsl_embed_limits() -> SettingsOut:
    """Restore BSL embedding char limits to built-in defaults."""
    runtime_settings.reset_bsl_embed_limits()
    return SettingsOut(**runtime_settings.get_all())


@router.get("/models")
def list_models() -> ModelsResponse:
    result = lm_studio.list_embedding_models()
    return ModelsResponse(
        ok=result["ok"],
        lm_studio_url=result["lm_studio_url"],
        source=result.get("source") or "",
        error=result.get("error"),
        default_embedding_model=runtime_settings.get_default_embedding_model(),
        models=result.get("models") or [],
    )


@router.get("/ping")
def ping_lm() -> dict:
    return lm_studio.ping()


@router.get("/build")
def build_info() -> dict:
    return {"build": APP_VERSION}


def _json_schema_type(schema: dict | None) -> str:
    if not schema or not isinstance(schema, dict):
        return ""
    if "anyOf" in schema or "oneOf" in schema:
        alts = schema.get("anyOf") or schema.get("oneOf") or []
        parts = [_json_schema_type(a) for a in alts if isinstance(a, dict)]
        parts = [p for p in parts if p and p != "null"]
        if not parts:
            return "null" if any(
                isinstance(a, dict) and a.get("type") == "null" for a in alts
            ) else ""
        base = " | ".join(dict.fromkeys(parts))
        if any(isinstance(a, dict) and a.get("type") == "null" for a in alts):
            return f"{base} | null"
        return base
    t = schema.get("type")
    if isinstance(t, list):
        return " | ".join(str(x) for x in t)
    if t == "array":
        items = _json_schema_type(schema.get("items") if isinstance(schema.get("items"), dict) else None)
        return f"array[{items}]" if items else "array"
    return str(t or "")


def _tool_params_from_schema(parameters: dict | None) -> list:
    from app.schemas.settings import McpToolParamOut

    if not parameters or not isinstance(parameters, dict):
        return []
    props = parameters.get("properties") or {}
    if not isinstance(props, dict):
        return []
    required = set(parameters.get("required") or [])
    out = []
    for name, schema in props.items():
        if not isinstance(schema, dict):
            schema = {}
        default = schema.get("default", None)
        default_s = None if default is None else str(default)
        out.append(
            McpToolParamOut(
                name=str(name),
                type=_json_schema_type(schema),
                required=name in required,
                description=str(schema.get("description") or ""),
                default=default_s,
            )
        )
    return out


@router.get("/tools")
async def list_mcp_tools(lang: str = "en") -> McpToolsResponse:
    """List MCP tools exposed to agents (name + description + parameters).

    ``lang=ru`` returns Russian titles/descriptions for the settings UI.
    Agent-facing MCP tool text stays English regardless.
    """
    from app.mcp.server import mcp
    from app.mcp.tool_i18n_ru import apply_tool_i18n_ru
    from app.schemas.settings import McpToolOut, McpToolParamOut

    want_ru = (lang or "en").strip().lower().startswith("ru")
    tools = await mcp.list_tools()
    call_chain_on = runtime_settings.get_experimental_call_chain()
    rows: list[McpToolOut] = []
    for tool in tools:
        title = ""
        ann = getattr(tool, "annotations", None)
        if ann is not None:
            title = str(getattr(ann, "title", None) or "") or ""
            if not title and isinstance(ann, dict):
                title = str(ann.get("title") or "")
        name = str(getattr(tool, "name", "") or "")
        # Hide experimental tools from settings UI when the flag is off.
        if name == "trace_call_chain" and not call_chain_on:
            continue
        description = str(getattr(tool, "description", None) or "").strip()
        parameters = _tool_params_from_schema(getattr(tool, "parameters", None))
        if want_ru:
            title, description, param_dicts = apply_tool_i18n_ru(
                name=name,
                title=title,
                description=description,
                parameters=parameters,
            )
            parameters = [McpToolParamOut(**d) for d in param_dicts]
        rows.append(
            McpToolOut(
                name=name,
                title=title,
                description=description,
                parameters=parameters,
            )
        )
    rows.sort(key=lambda r: r.name.lower())
    return McpToolsResponse(count=len(rows), tools=rows)


@router.get("/db")
def db_info() -> DbInfoOut:
    db = Path(settings.db_path)
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    return DbInfoOut(
        db_path=str(db),
        db_bytes=db.stat().st_size if db.exists() else 0,
        wal_bytes=(wal.stat().st_size if wal.exists() else 0)
        + (shm.stat().st_size if shm.exists() else 0),
        zvec_bytes=_dir_size(Path(settings.zvec_dir)),
    )


@router.post("/vacuum")
def vacuum_db() -> VacuumResponse:
    """Compact SQLite (VACUUM) after WAL checkpoint. Prefer when UI is idle."""
    db = Path(settings.db_path)
    before = db.stat().st_size if db.exists() else 0
    conn = connect(settings.db_path)
    try:
        busy = conn.execute(
            """
            SELECT COUNT(*) AS n FROM entities
            WHERE status IN ('parsing', 'indexing', 'loading_modules')
            """
        ).fetchone()
        if int(busy["n"] or 0):
            raise HTTPException(409, "VACUUM blocked: parse or index is running")
        # VACUUM cannot run inside an open transaction
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.isolation_level = None
        conn.execute("VACUUM")
        conn.isolation_level = ""
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"VACUUM failed: {exc}") from exc
    finally:
        conn.close()
    after = db.stat().st_size if db.exists() else 0
    saved = max(0, before - after)
    return VacuumResponse(
        ok=True,
        before_bytes=before,
        after_bytes=after,
        db_path=str(db),
        detail=f"freed {saved} bytes" if saved else "no size change",
    )
