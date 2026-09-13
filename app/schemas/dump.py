from __future__ import annotations

from pydantic import BaseModel, Field


class DumpProfileOut(BaseModel):
    id: int
    name: str
    comment: str = ""
    target_type: str = "configuration"
    extension_name: str = ""
    ib_type: str = "file"
    ib_address: str = ""
    ib_user: str = ""
    has_password: bool = False
    platform_path_override: str = ""
    out_dir: str = ""
    dump_mode: str = "update"
    clear_before_full: bool = False
    post_action: str = ""
    entity_id: int | None = None
    timeout_sec: int = 7200
    last_run_state: str = ""
    last_run_at: str = ""
    created_at: str = ""
    updated_at: str = ""


class DumpProfileCreate(BaseModel):
    name: str
    comment: str = ""
    target_type: str = "configuration"
    extension_name: str = ""
    ib_type: str = "file"
    ib_address: str = ""
    ib_user: str = ""
    password: str | None = None
    platform_path_override: str = ""
    out_dir: str = ""
    dump_mode: str = "update"
    clear_before_full: bool = False
    post_action: str = ""
    entity_id: int | None = None
    timeout_sec: int = 7200


class DumpProfilePatch(BaseModel):
    name: str | None = None
    comment: str | None = None
    target_type: str | None = None
    extension_name: str | None = None
    ib_type: str | None = None
    ib_address: str | None = None
    ib_user: str | None = None
    password: str | None = Field(
        default=None,
        description="Omit to keep secret; non-empty replaces; empty string deletes",
    )
    platform_path_override: str | None = None
    out_dir: str | None = None
    dump_mode: str | None = None
    clear_before_full: bool | None = None
    post_action: str | None = None
    entity_id: int | None = None
    timeout_sec: int | None = None


class DumpRunOut(BaseModel):
    id: int
    profile_id: int
    state: str
    started_at: str = ""
    finished_at: str = ""
    return_code: int | None = None
    error_reason: str = ""
    log_path: str = ""
    log_tail: str = ""
    ingest_state: str = ""
    bootstrap_note: str = ""
    file_count: int = 0
    last_activity_at: float = 0


class DumpQueueRunOut(DumpRunOut):
    profile_name: str = ""
    queue_position: int = 0


class DumpQueueOut(BaseModel):
    running: DumpQueueRunOut | None = None
    pending: list[DumpQueueRunOut] = Field(default_factory=list)


class DumpQueueReorderIn(BaseModel):
    run_ids: list[int] = Field(default_factory=list)


class DumpRunIn(BaseModel):
    submit_parse: bool = True


class DumpRunBatchIn(BaseModel):
    profile_ids: list[int] = Field(min_length=1)
    submit_parse: bool = True


class DumpRunCreated(BaseModel):
    run_id: int
    profile_id: int


class PreflightOut(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DumpProfileCopyIn(BaseModel):
    name: str | None = Field(
        default=None,
        description="Optional name for the copy; auto-suffixed (_2, _3, …) when omitted",
    )


class DumpCreateContextIn(BaseModel):
    name: str | None = None
    comment: str | None = None
    ingest_profile: str | None = None


class DumpProfilePreflightIn(BaseModel):
    password: str | None = Field(
        default=None,
        description="Optional password override when checking an edited saved profile",
    )


class DumpListExtensionsIn(BaseModel):
    ib_type: str = "file"
    ib_address: str = ""
    ib_user: str = ""
    password: str | None = None
    platform_path_override: str = ""


class DumpExtensionsOut(BaseModel):
    extensions: list[str] = Field(default_factory=list)
    error: str = ""
