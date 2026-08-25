from __future__ import annotations

from pydantic import BaseModel, Field


class BoundField(BaseModel):
    min: int
    max: int
    default: int


class BslEmbedLimitsBounds(BaseModel):
    passage_max_chars: BoundField
    chunk_size: BoundField
    chunk_overlap: BoundField
    min_body_chars: BoundField
    max_chunks: BoundField


class BslEmbedLimitsDefaults(BaseModel):
    passage_max_chars: int
    chunk_size: int
    chunk_overlap: int
    min_body_chars: int
    max_chunks: int


class BslEmbedWindowPreset(BaseModel):
    id: str
    tokens: int
    models_hint: str = ""
    limits: BslEmbedLimitsDefaults


class SettingsOut(BaseModel):
    lm_studio_url: str
    default_embedding_model: str
    embedding_workers: int = 2
    bsl_embed_mode: str = "meta"
    ui_poll_interval_sec: int = 2
    ui_show_objects_col: bool = True
    ui_show_model_col: bool = False
    stats_window_sec: int = 1200
    log_level: str = "minimal"
    experimental_call_chain: bool = True
    app_log_enabled: bool = True
    slow_request_ms: int = 3000
    app_log_retain_days: int = 14
    app_log_max_rows: int = 5000
    bsl_passage_max_chars: int = 1350
    bsl_chunk_size: int = 1100
    bsl_chunk_overlap: int = 130
    bsl_min_body_chars: int = 200
    bsl_max_chunks: int = 12
    bsl_embed_limits_defaults: BslEmbedLimitsDefaults | None = None
    bsl_embed_limits_bounds: BslEmbedLimitsBounds | None = None
    bsl_embed_window_presets: list[BslEmbedWindowPreset] | None = None


class SettingsPatch(BaseModel):
    lm_studio_url: str | None = Field(default=None, description="LM Studio base URL, e.g. http://127.0.0.1:1234")
    default_embedding_model: str | None = Field(default=None, description="Default embedding model id")
    embedding_workers: int | None = Field(default=None, description="Parallel embedding workers: 1, 2, 4, 8, 12 or 16")
    bsl_embed_mode: str | None = Field(
        default=None,
        description="Default BSL embedding for new uploads: meta | body | chunks",
    )
    ui_poll_interval_sec: int | None = Field(
        default=None,
        description="UI entity list auto-refresh period in seconds; 0 disables",
    )
    ui_show_objects_col: bool | None = Field(
        default=None, description="Show Objects column in entity table"
    )
    ui_show_model_col: bool | None = Field(
        default=None, description="Show Model column in entity table"
    )
    stats_window_sec: int | None = Field(
        default=None,
        description="In-memory usage stats retention window in seconds (1–60 min steps)",
    )
    log_level: str | None = Field(
        default=None,
        description="Application log detail: minimal | errors | verbose",
    )
    experimental_call_chain: bool | None = Field(
        default=None,
        description=(
            "Enable MCP tool trace_call_chain (BSL calls BFS). Default on; "
            "not shown in UI — PATCH /api/settings to toggle"
        ),
    )
    app_log_enabled: bool | None = Field(
        default=None,
        description="Persist errors, slow MCP, and parse/reindex summaries to SQLite app_log",
    )
    slow_request_ms: int | None = Field(
        default=None, description="Duration threshold (ms) for slow request log (500–60000)"
    )
    app_log_retain_days: int | None = Field(
        default=None, description="Days to keep persisted app_log rows (1–90)"
    )
    app_log_max_rows: int | None = Field(
        default=None, description="Max rows in app_log (100–50000); oldest pruned"
    )
    bsl_passage_max_chars: int | None = Field(
        default=None, description="Max chars for one embedding passage (meta+body)"
    )
    bsl_chunk_size: int | None = Field(
        default=None, description="Body window size in chars for chunks mode"
    )
    bsl_chunk_overlap: int | None = Field(
        default=None, description="Overlap between consecutive body chunks"
    )
    bsl_min_body_chars: int | None = Field(
        default=None, description="Minimum body chars reserved when truncating meta"
    )
    bsl_max_chunks: int | None = Field(
        default=None, description="Max chunk passages per method"
    )


class EmbeddingModelOut(BaseModel):
    id: str
    state: str = "unknown"
    publisher: str = ""
    quantization: str = ""
    max_context_length: int | None = None


class ModelsResponse(BaseModel):
    ok: bool
    lm_studio_url: str
    source: str = ""
    error: str | None = None
    default_embedding_model: str
    models: list[EmbeddingModelOut]


class VacuumResponse(BaseModel):
    ok: bool
    before_bytes: int
    after_bytes: int
    db_path: str
    detail: str = ""


class DbInfoOut(BaseModel):
    db_path: str
    db_bytes: int
    wal_bytes: int = 0
    zvec_bytes: int = 0


class McpToolParamOut(BaseModel):
    name: str
    type: str = ""
    required: bool = False
    description: str = ""
    default: str | None = None


class McpToolOut(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    parameters: list[McpToolParamOut] = []


class McpToolsResponse(BaseModel):
    count: int
    tools: list[McpToolOut]
