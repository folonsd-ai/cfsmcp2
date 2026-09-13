from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.core.config import settings
from app.core.database import connect
from app.schemas.dump import (
    DumpCreateContextIn,
    DumpExtensionsOut,
    DumpListExtensionsIn,
    DumpProfileCopyIn,
    DumpProfileCreate,
    DumpProfileOut,
    DumpProfilePatch,
    DumpProfilePreflightIn,
    DumpQueueOut,
    DumpQueueReorderIn,
    DumpQueueRunOut,
    DumpRunBatchIn,
    DumpRunCreated,
    DumpRunIn,
    DumpRunOut,
    PreflightOut,
)
from app.services import dump_store, runtime_settings
from app.services.dump_ingest import create_entity_from_dump_dir
from app.services.dump_worker import get_worker
from app.services.pipeline import parse_entity
from app.services import jobs
from app.services.mounts import effective_runtime_mode
from app.services.onec_dump import DumpProfileSpec, list_configuration_extensions, preflight_dump

router = APIRouter(prefix="/api/dump", tags=["dump"])

_DUMP_UNAVAILABLE = "dump_unavailable_in_docker"


def _require_native_dump() -> None:
    if effective_runtime_mode() == "docker":
        raise HTTPException(
            501,
            detail={
                "message_key": _DUMP_UNAVAILABLE,
                "detail": "Выгрузка конфигурации недоступна в Docker-режиме",
            },
        )


def _create_body_to_spec(body: DumpProfileCreate) -> DumpProfileSpec:
    override = (body.platform_path_override or "").strip()
    platform = override or runtime_settings.get_onec_platform_path()
    return DumpProfileSpec(
        name=body.name,
        target_type=body.target_type,
        extension_name=body.extension_name,
        ib_type=body.ib_type,
        ib_address=body.ib_address,
        ib_user=body.ib_user,
        password=body.password or "",
        platform_path=platform,
        out_dir=body.out_dir,
        dump_mode=body.dump_mode,
        clear_before_full=body.clear_before_full,
        timeout_sec=body.timeout_sec,
    )


def _preflight_response(spec: DumpProfileSpec) -> PreflightOut:
    result = preflight_dump(spec)
    return PreflightOut(ok=result.ok, errors=result.errors, warnings=result.warnings)


def _list_extensions_spec(body: DumpListExtensionsIn) -> DumpProfileSpec:
    override = (body.platform_path_override or "").strip()
    platform = override or runtime_settings.get_onec_platform_path()
    return DumpProfileSpec(
        ib_type=body.ib_type,
        ib_address=body.ib_address,
        ib_user=body.ib_user,
        password=body.password or "",
        platform_path=platform,
    )


def _list_extensions_response(spec: DumpProfileSpec) -> DumpExtensionsOut:
    try:
        extensions, error = list_configuration_extensions(spec)
    except ValueError as exc:
        return DumpExtensionsOut(error=str(exc))
    return DumpExtensionsOut(extensions=extensions, error=error)


@router.get("/profiles", response_model=list[DumpProfileOut])
def list_profiles() -> list[DumpProfileOut]:
    conn = connect(settings.db_path)
    try:
        return [DumpProfileOut(**row) for row in dump_store.list_profiles(conn)]
    finally:
        conn.close()


@router.post("/profiles", response_model=DumpProfileOut, status_code=201)
def create_profile(body: DumpProfileCreate) -> DumpProfileOut:
    _require_native_dump()
    conn = connect(settings.db_path)
    try:
        row = dump_store.create_profile(conn, body.model_dump())
        return DumpProfileOut(**row)
    finally:
        conn.close()


@router.get("/profiles/{profile_id}", response_model=DumpProfileOut)
def get_profile(profile_id: int) -> DumpProfileOut:
    conn = connect(settings.db_path)
    try:
        row = dump_store.get_profile(conn, profile_id)
        if not row:
            raise HTTPException(404, "profile not found")
        return DumpProfileOut(**row)
    finally:
        conn.close()


@router.patch("/profiles/{profile_id}", response_model=DumpProfileOut)
def patch_profile(profile_id: int, body: DumpProfilePatch) -> DumpProfileOut:
    _require_native_dump()
    conn = connect(settings.db_path)
    try:
        data = body.model_dump(exclude_unset=True)
        row = dump_store.update_profile(conn, profile_id, data)
        if not row:
            raise HTTPException(404, "profile not found")
        return DumpProfileOut(**row)
    finally:
        conn.close()


@router.post("/profiles/{profile_id}/copy", response_model=DumpProfileOut, status_code=201)
def copy_profile(profile_id: int, body: DumpProfileCopyIn | None = None) -> DumpProfileOut:
    _require_native_dump()
    conn = connect(settings.db_path)
    try:
        try:
            row = dump_store.copy_profile(
                conn,
                profile_id,
                name=body.name if body else None,
            )
        except ValueError as exc:
            if str(exc) == "name_taken":
                raise HTTPException(409, "profile name already exists") from exc
            raise HTTPException(400, str(exc)) from exc
        if not row:
            raise HTTPException(404, "profile not found")
        return DumpProfileOut(**row)
    finally:
        conn.close()


@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: int) -> None:
    _require_native_dump()
    conn = connect(settings.db_path)
    try:
        if not dump_store.delete_profile(conn, profile_id):
            raise HTTPException(404, "profile not found")
    finally:
        conn.close()


@router.post("/preflight", response_model=PreflightOut)
def preflight_unsaved(body: DumpProfileCreate) -> PreflightOut:
    _require_native_dump()
    return _preflight_response(_create_body_to_spec(body))


@router.post("/list-extensions", response_model=DumpExtensionsOut)
def list_extensions_unsaved(body: DumpListExtensionsIn) -> DumpExtensionsOut:
    _require_native_dump()
    return _list_extensions_response(_list_extensions_spec(body))


@router.post("/profiles/{profile_id}/preflight", response_model=PreflightOut)
def preflight_profile(
    profile_id: int,
    body: DumpProfilePreflightIn | None = None,
) -> PreflightOut:
    _require_native_dump()
    conn = connect(settings.db_path)
    try:
        row = dump_store.get_profile_row(conn, profile_id)
        if not row:
            raise HTTPException(404, "profile not found")
        pwd_override = body.password if body and body.password is not None else None
        spec = dump_store.profile_to_spec(
            row,
            platform_path=runtime_settings.get_onec_platform_path(),
            password=pwd_override,
        )
        return _preflight_response(spec)
    finally:
        conn.close()


@router.post("/profiles/{profile_id}/list-extensions", response_model=DumpExtensionsOut)
def list_extensions_profile(
    profile_id: int,
    body: DumpProfilePreflightIn | None = None,
) -> DumpExtensionsOut:
    _require_native_dump()
    conn = connect(settings.db_path)
    try:
        row = dump_store.get_profile_row(conn, profile_id)
        if not row:
            raise HTTPException(404, "profile not found")
        pwd_override = body.password if body and body.password is not None else None
        spec = dump_store.profile_to_spec(
            row,
            platform_path=runtime_settings.get_onec_platform_path(),
            password=pwd_override,
        )
        return _list_extensions_response(spec)
    finally:
        conn.close()


@router.post("/profiles/{profile_id}/run", response_model=DumpRunCreated, status_code=202)
def run_profile(profile_id: int, body: DumpRunIn | None = None) -> DumpRunCreated:
    _require_native_dump()
    submit_parse = True if body is None else body.submit_parse
    conn = connect(settings.db_path)
    try:
        if not dump_store.get_profile(conn, profile_id):
            raise HTTPException(404, "profile not found")
        try:
            run_id = dump_store.create_run(conn, profile_id, submit_parse=submit_parse)
        except ValueError as exc:
            if str(exc) == "active_run_exists":
                raise HTTPException(409, "profile already has queued or running dump") from exc
            raise
        get_worker().start()
        return DumpRunCreated(run_id=run_id, profile_id=profile_id)
    finally:
        conn.close()


@router.post("/run-batch", response_model=list[DumpRunCreated], status_code=202)
def run_batch(body: DumpRunBatchIn) -> list[DumpRunCreated]:
    _require_native_dump()
    conn = connect(settings.db_path)
    created: list[DumpRunCreated] = []
    try:
        for pid in body.profile_ids:
            if not dump_store.get_profile(conn, pid):
                raise HTTPException(404, f"profile {pid} not found")
            try:
                run_id = dump_store.create_run(
                    conn, pid, submit_parse=body.submit_parse
                )
            except ValueError as exc:
                if str(exc) == "active_run_exists":
                    raise HTTPException(409, f"profile {pid} already has queued or running dump") from exc
                raise
            created.append(DumpRunCreated(run_id=run_id, profile_id=pid))
        get_worker().start()
        return created
    finally:
        conn.close()


def _queue_out(raw: dict) -> DumpQueueRunOut:
    return DumpQueueRunOut(
        **{k: raw[k] for k in DumpRunOut.model_fields if k in raw},
        profile_name=str(raw.get("profile_name") or ""),
        queue_position=int(raw.get("queue_position") or 0),
    )


@router.get("/queue", response_model=DumpQueueOut)
def get_dump_queue() -> DumpQueueOut:
    conn = connect(settings.db_path)
    try:
        snap = dump_store.list_queue(conn)
        running = snap.get("running")
        pending = snap.get("pending") or []
        return DumpQueueOut(
            running=_queue_out(running) if running else None,
            pending=[_queue_out(row) for row in pending],
        )
    finally:
        conn.close()


@router.patch("/queue/order", response_model=DumpQueueOut)
def reorder_dump_queue(body: DumpQueueReorderIn) -> DumpQueueOut:
    _require_native_dump()
    conn = connect(settings.db_path)
    try:
        try:
            dump_store.reorder_queued_runs(conn, body.run_ids)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        snap = dump_store.list_queue(conn)
        running = snap.get("running")
        pending = snap.get("pending") or []
        return DumpQueueOut(
            running=_queue_out(running) if running else None,
            pending=[_queue_out(row) for row in pending],
        )
    finally:
        conn.close()


@router.get("/runs", response_model=list[DumpRunOut])
def list_runs(
    profile_id: int | None = Query(default=None),
    entity_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[DumpRunOut]:
    conn = connect(settings.db_path)
    try:
        rows = dump_store.list_runs(
            conn, profile_id=profile_id, entity_id=entity_id, limit=limit
        )
        return [DumpRunOut(**row) for row in rows]
    finally:
        conn.close()


@router.post("/profiles/{profile_id}/create-context", response_model=DumpProfileOut)
def create_context_from_dump(profile_id: int, body: DumpCreateContextIn) -> DumpProfileOut:
    """Explicit «Создать контекст из этой выгрузки» — not auto on dump finish."""
    _require_native_dump()
    conn = connect(settings.db_path)
    try:
        row = dump_store.get_profile_row(conn, profile_id)
        if not row:
            raise HTTPException(404, "profile not found")
        if row["entity_id"]:
            raise HTTPException(409, "profile already linked to an entity")
        out_dir = (row["out_dir"] or "").strip()
        if not out_dir:
            raise HTTPException(400, "out_dir is empty")
        entity_id = create_entity_from_dump_dir(
            conn,
            out_dir=out_dir,
            name=(body.name or "").strip(),
            comment=body.comment,
            ingest_profile_json=body.ingest_profile,
        )
        updated = dump_store.update_profile(
            conn,
            profile_id,
            {"entity_id": entity_id, "post_action": "import"},
        )
        conn.commit()
        jobs.submit(parse_entity, entity_id)
        assert updated is not None
        return DumpProfileOut(**updated)
    finally:
        conn.close()


@router.get("/runs/{run_id}", response_model=DumpRunOut)
def get_run(run_id: int) -> DumpRunOut:
    conn = connect(settings.db_path)
    try:
        row = dump_store.get_run(conn, run_id)
        if not row:
            raise HTTPException(404, "run not found")
        return DumpRunOut(**row)
    finally:
        conn.close()


@router.post("/runs/{run_id}/cancel", response_model=DumpRunOut)
def cancel_run(run_id: int) -> DumpRunOut:
    _require_native_dump()
    if not get_worker().cancel_run(run_id):
        conn = connect(settings.db_path)
        try:
            row = dump_store.get_run(conn, run_id)
            if not row:
                raise HTTPException(404, "run not found")
            raise HTTPException(409, f"run is {row['state']}, cannot cancel")
        finally:
            conn.close()
    conn = connect(settings.db_path)
    try:
        row = dump_store.get_run(conn, run_id)
        if not row:
            raise HTTPException(404, "run not found")
        return DumpRunOut(**row)
    finally:
        conn.close()
