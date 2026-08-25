from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import entities as entities_api
from app.api import health as health_api
from app.api import settings as settings_api
from app.api import stats as stats_api
from app.api import system as system_api
from app.api import tags as tags_api
from app.core.config import settings
from app.core.database import init_db
from app.core.version import APP_VERSION
from app.mcp.server import mcp
from app.services.pipeline import backfill_entity_types, cleanup_metadata_orphans, recover_interrupted_indexing
from app.services.usage_stats import is_important_api, is_ui_poll_api, usage_stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("cfsmcp2")

mcp_app = mcp.http_app(path="/", transport="streamable-http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.metadata_dir.mkdir(parents=True, exist_ok=True)
    settings.dumps_dir.mkdir(parents=True, exist_ok=True)
    settings.zvec_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(settings.db_path)
    cleanup_metadata_orphans()
    backfill_entity_types()
    n_resume = recover_interrupted_indexing()
    if n_resume:
        log.info("resumed %s interrupted indexing job(s)", n_resume)
    async with mcp_app.lifespan(app):
        usage_stats.record_lifecycle(
            "start",
            f"host={settings.host}:{settings.port} mcp=/mcp",
        )
        log.info("cfsmcp2 ready on %s:%s mcp=/mcp", settings.host, settings.port)
        yield
        usage_stats.record_lifecycle("stop", "app shutdown")


def create_app() -> FastAPI:
    api = FastAPI(title="cfsmcp2", version=APP_VERSION, lifespan=lifespan)
    api.include_router(health_api.router)
    api.include_router(entities_api.router)
    api.include_router(tags_api.router)
    api.include_router(settings_api.router)
    api.include_router(stats_api.router)
    api.include_router(system_api.router)

    @api.middleware("http")
    async def mcp_trailing_slash(request: Request, call_next):
        # Cursor MCP client does not follow 307 /mcp -> /mcp/
        if request.scope.get("path") == "/mcp":
            request.scope["path"] = "/mcp/"
        return await call_next(request)

    @api.middleware("http")
    async def track_api_usage(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path.startswith("/api/stats"):
            return await call_next(request)
        name_path = path
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] == "api" and parts[2] == "entities" and parts[3].isdigit():
            parts[3] = "{id}"
            name_path = "/".join(parts)
        important = is_important_api(request.method, name_path)
        t0 = time.perf_counter()
        ok = True
        detail = ""
        status = 0
        try:
            response = await call_next(request)
            status = response.status_code
            ok = status < 400
            if not ok:
                detail = f"status={status}"
            return response
        except Exception as exc:
            ok = False
            detail = str(exc)[:800]
            raise
        finally:
            ms = (time.perf_counter() - t0) * 1000
            if important:
                usage_stats.record(
                    kind="api",
                    name=f"{request.method} {name_path}",
                    ok=ok,
                    duration_ms=ms,
                    detail=detail,
                    tier="usage",
                    persist=False,
                )
                try:
                    from app.services import app_log as app_log_svc

                    replay = app_log_svc.format_api_replay_detail(
                        method=request.method,
                        path=name_path,
                        duration_ms=ms,
                        query=str(request.url.query or ""),
                        status=status,
                        detail=detail,
                        ok=ok,
                    )
                    if not ok:
                        app_log_svc.append_error(
                            kind="api",
                            name=f"{request.method} {name_path}",
                            detail=replay,
                            duration_ms=ms,
                        )
                    else:
                        app_log_svc.maybe_slow(
                            kind="api",
                            name=f"{request.method} {name_path}",
                            duration_ms=ms,
                            detail=replay,
                            ok=True,
                        )
                except Exception:
                    pass
                if not ok:
                    usage_stats.record_error_detail(
                        kind="api",
                        name=f"{request.method} {name_path}",
                        detail=detail or f"status={status}",
                        duration_ms=ms,
                        persist=False,
                    )
            elif not ok:
                usage_stats.record(
                    kind="api",
                    name=f"{request.method} {name_path}",
                    ok=False,
                    duration_ms=ms,
                    detail=detail or f"status={status}",
                    tier="error",
                )
            elif is_ui_poll_api(request.method, name_path):
                # Board auto-refresh — keep out of verbose app-log
                pass
            else:
                usage_stats.record(
                    kind="api",
                    name=f"{request.method} {name_path}",
                    ok=True,
                    duration_ms=ms,
                    detail=detail,
                    tier="verbose",
                )

    static_dir = Path(__file__).parent / "static"
    api.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @api.get("/")
    def index():
        # Avoid stale UI after deploys (LM settings / advanced tab handlers).
        return FileResponse(
            static_dir / "index.html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    api.mount("/mcp", mcp_app)
    return api


app = create_app()


def run() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
