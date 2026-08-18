from __future__ import annotations

import time

from pydantic import BaseModel, computed_field

# Профиль ingest по умолчанию = UI «Полная загрузка».
# Сохраняется на сущности как JSON; смена профиля = только через новую сущность.
_EXCLUDE_DEFAULT = [
    "АрхивныеАлгоритмы",
    "РегламентированныйОтчет",
    "Драйвер",
    "КомпонентаИнтеграции",
]

# bsl_load_mode:
# - signatures_only — заголовки, без тел, без рёбер calls (быстрый)
# - signatures — заголовки, без тел, с calls (тело в памяти при ingest)
# - full — тела (+ #Область) + calls
# Legacy: code → full. Старый signatures (= с calls) сохраняет значение.
BSL_LOAD_MODES = frozenset({"signatures_only", "signatures", "full"})


def normalize_bsl_load_mode(mode: str | None) -> str:
    m = str(mode or "").strip().lower().replace("-", "_")
    if m in ("signatures_only", "headers", "sig_only", "signatures_nocalls"):
        return "signatures_only"
    if m in ("signatures_calls", "sig_calls"):
        return "signatures"
    if m == "code":
        return "full"
    if m in BSL_LOAD_MODES:
        return m
    return "signatures"


def bsl_stores_body(mode: str | None) -> bool:
    return normalize_bsl_load_mode(mode) == "full"


def bsl_extracts_calls(mode: str | None) -> bool:
    return normalize_bsl_load_mode(mode) in ("signatures", "full")


DEFAULT_INGEST_PROFILE: dict[str, object] = {
    "metadata": True,
    "bsl": True,
    "dcs": True,
    "spreadsheet_templates": True,
    "managed_forms": True,
    "ordinary_forms": True,
    "help": True,
    "exclude_name_substrings": list(_EXCLUDE_DEFAULT),
    "bsl_load_mode": "full",
    "bsl_embed_mode": "chunks",
}

# UI-пресет «Модули»: meta + BSL с calls.
MODULES_INGEST_PROFILE: dict[str, object] = {
    "metadata": True,
    "bsl": True,
    "dcs": False,
    "spreadsheet_templates": False,
    "managed_forms": False,
    "ordinary_forms": False,
    "help": False,
    "exclude_name_substrings": list(_EXCLUDE_DEFAULT),
    "bsl_load_mode": "signatures",
    "bsl_embed_mode": "meta",
}

# UI-пресет «Только метаданные».
LEAN_INGEST_PROFILE: dict[str, object] = {
    "metadata": True,
    "bsl": False,
    "dcs": False,
    "spreadsheet_templates": False,
    "managed_forms": False,
    "ordinary_forms": False,
    "help": False,
    "exclude_name_substrings": list(_EXCLUDE_DEFAULT),
    "bsl_load_mode": "signatures_only",
    "bsl_embed_mode": "meta",
}


def is_lean_ingest_profile(profile: dict | None) -> bool:
    """True если профиль не индексирует BSL/справку/формы/СКД/макеты."""
    p = profile or {}
    return (
        p.get("bsl") is False
        and not p.get("dcs")
        and not p.get("spreadsheet_templates")
        and not p.get("managed_forms")
        and not p.get("ordinary_forms")
        and p.get("help") is False
    )


def ingest_profile_flags(profile: dict | None) -> dict[str, object]:
    """Компактные флаги профиля для list_contexts / UI."""
    p = profile or {}
    lean = is_lean_ingest_profile(p)
    return {
        "bsl": p.get("bsl") is not False,
        "help": p.get("help") is not False,
        "dcs": bool(p.get("dcs")),
        "managed_forms": bool(p.get("managed_forms")),
        "ordinary_forms": bool(p.get("ordinary_forms")),
        "spreadsheet_templates": bool(p.get("spreadsheet_templates")),
        "lean": lean,
    }


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "…"
    sec = max(0, int(round(seconds)))
    if sec < 60:
        return f"~{sec}s"
    if sec < 3600:
        m, s = divmod(sec, 60)
        return f"~{m}m" if s < 15 else f"~{m}m{s:02d}s"
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return f"~{h}h{m:02d}m"


class EntityOut(BaseModel):
    id: int
    name: str
    synonym: str = ""
    comment: str = ""
    entity_type: str = "configuration"
    version: str = ""
    file_path: str = ""
    source_mode: str = "report"
    source_location: str = "upload"
    source_path: str = ""
    ingest_profile: dict = {}
    dumps_dir: str = ""
    zip_path: str = ""
    enabled: bool
    bsl_enabled: bool = True
    bsl_method_count: int = 0
    bsl_load_mode: str = ""
    bsl_embed_mode: str = ""
    tag_ids: list[int] = []
    model: str = ""
    status: str
    name_locked: bool = False
    object_count: int = 0
    link_count: int = 0
    indexed_count: int = 0
    index_target: int = 0
    index_started_at: float = 0
    index_scope: str = ""
    parse_added: int = 0
    parse_changed: int = 0
    parse_deleted: int = 0
    parse_unchanged: int = 0
    error_message: str = ""
    usage_calls: int = 0
    usage_share_pct: int = 0
    usage_bar_pct: int = 0
    usage_window_sec: int = 1200

    @computed_field
    @property
    def progress_pct(self) -> int:
        if self.status == "ready":
            return 100
        if self.status == "indexing":
            target = self.index_target or self.object_count or 0
            if target <= 0:
                return 100
            if self.indexed_count >= target:
                return 100
            return min(99, int(100 * self.indexed_count / target))
        if self.status == "parsing":
            target = self.index_target or 0
            if target <= 0:
                return 0
            # Во время парсинга indexed_count хранит проценты (index_target=100).
            if target == 100:
                return min(99, max(0, int(self.indexed_count or 0)))
            return min(99, int(100 * (self.indexed_count or 0) / target))
        if self.status == "loading_modules":
            return 50
        return 0

    @computed_field
    @property
    def eta_sec(self) -> int | None:
        if self.status != "indexing":
            return None
        target = self.index_target or self.object_count or 0
        done = self.indexed_count or 0
        if target <= 0:
            return 0
        remaining = target - done
        if remaining <= 0:
            return 0
        started = float(self.index_started_at or 0)
        if started <= 0 or done <= 0:
            return None
        elapsed = time.time() - started
        if elapsed < 0.8:
            return None
        rate = done / elapsed
        if rate <= 0:
            return None
        return max(0, int(round(remaining / rate)))

    @computed_field
    @property
    def parse_delta(self) -> str:
        return (
            f"+{self.parse_added}~{self.parse_changed}"
            f"-{self.parse_deleted}={self.parse_unchanged}"
        )

    @property
    def _has_parse_stats(self) -> bool:
        return bool(
            self.parse_added
            or self.parse_changed
            or self.parse_deleted
            or self.parse_unchanged
        )

    @computed_field
    @property
    def progress_text(self) -> str:
        if self.status == "indexing":
            target = self.index_target or self.object_count or 0
            eta = format_eta(self.eta_sec)
            left = max(0, target - (self.indexed_count or 0))
            return f"{self.indexed_count}/{target} · {eta} · {left} left"
        if self.status == "parsing":
            pct = self.progress_pct
            return f"{pct}% · {self.object_count} objs · {self.link_count} links"
        if self.status == "loading_modules":
            return "BSL modules…"
        if self.status == "ready":
            if self._has_parse_stats:
                return f"{self.object_count} · {self.parse_delta}"
            return f"{self.object_count} objects"
        if self.status == "parsed":
            if self._has_parse_stats:
                return f"{self.object_count} · {self.parse_delta} · reindex"
            return f"{self.object_count} · reindex"
        if self.status == "uploaded":
            return "waiting"
        if self.status in {"parse_error", "index_error", "modules_error"}:
            return self.error_message or self.status
        return self.status


class EntityPatch(BaseModel):
    enabled: bool | None = None
    # Устарело как отдельный выключатель: PATCH синхронизирует с enabled (один toggle строки).
    bsl_enabled: bool | None = None
    tag_ids: list[int] | None = None
    model: str | None = None
    comment: str | None = None
    name: str | None = None
    # Только путь (без ingest). Path-режим: также file_path/dumps_dir; upload — bookmark source_path.
    source_path: str | None = None


class ImportPathRequest(BaseModel):
    """Body of POST /api/entities/import-path (source_location=path, этап 2.5).

    Без файлового upload: ``source_path`` — абсолютный путь к файлу на диске
    сервера (для report — txt «Отчёт по конфигурации»). Файл не копируется,
    ``file_path``/``source_path`` = внешний путь; при delete файл не удаляется.
    """

    source_path: str
    source_mode: str = ""
    name: str | None = None
    comment: str | None = None
    merge: bool = False
    entity_id: int | None = None
    ingest_profile: str | None = None
    model: str | None = None


class EntityCopy(BaseModel):
    name: str


class ReindexResponse(BaseModel):
    id: int
    status: str
    detail: str = ""
    indexed_count: int = 0
    object_count: int = 0
    progress_pct: int = 0
