"""Post-dump ingest: import-path inside process (stage 7)."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.repositories import entities as ent_repo
from app.services import jobs, runtime_settings
from app.services.dump_identity import INGEST_BLOCKED, INGEST_DONE, INGEST_PENDING
from app.services.dump_parser import read_configuration_meta
from app.services.pipeline import parse_entity

log = logging.getLogger("cfsmcp2.dump_ingest")

ENTITY_BUSY_STATUSES = frozenset({"parsing", "indexing", "uploaded", "loading_modules"})
DEFERRED_MSG = "контекст занят, обновление отложено"


@dataclass(frozen=True, slots=True)
class PostIngestResult:
    ingest_state: str
    error_reason: str = ""


def post_action_triggers_ingest(post_action: str) -> bool:
    pa = (post_action or "").strip().lower()
    return pa in ("import", "import+reindex")


def import_dump_dir_to_entity(
    conn: sqlite3.Connection,
    *,
    out_dir: str | Path,
    entity_id: int,
) -> None:
    """Refresh linked entity from dump directory (same DB effect as import-path)."""
    from app.api.entities import _remove_server_dump_artifacts

    resolved = Path(out_dir).resolve()
    if not resolved.is_dir():
        raise ValueError(f"каталог выгрузки не найден: {resolved}")
    if not (resolved / "Configuration.xml").is_file():
        raise ValueError(f"нет Configuration.xml в {resolved}")

    existing = ent_repo.get_entity(conn, entity_id)
    if not existing:
        raise ValueError("привязанная сущность не найдена")

    meta = read_configuration_meta(resolved)
    _remove_server_dump_artifacts(existing)
    ent_repo.refresh_entity_file(
        conn,
        entity_id,
        synonym=meta.config_synonym or existing.get("synonym") or "",
        version=meta.version or existing.get("version") or "",
        file_path=str(resolved),
        model=existing.get("model") or runtime_settings.get_default_embedding_model(),
        entity_type=meta.entity_type or "configuration",
        source_path=str(resolved),
        source_location="path",
        source_mode="dump",
        dumps_dir=str(resolved),
        zip_path="",
        comment=existing.get("comment"),
    )


def create_entity_from_dump_dir(
    conn: sqlite3.Connection,
    *,
    out_dir: str | Path,
    name: str,
    comment: str | None = None,
    ingest_profile_json: str | None = None,
) -> int:
    """Create entity from dump out_dir (explicit user action only)."""
    from app.api.entities import _merged_ingest_profile, _raise_if_name_taken

    resolved = Path(out_dir).resolve()
    if not (resolved / "Configuration.xml").is_file():
        raise ValueError(f"нет Configuration.xml в {resolved}")

    meta = read_configuration_meta(resolved)
    tentative = (name or "").strip() or (meta.config_name or "").strip() or resolved.name
    profile = _merged_ingest_profile(ingest_profile_json)
    _raise_if_name_taken(
        conn,
        tentative,
        report_name=meta.config_name or tentative,
        report_synonym=meta.config_synonym or "",
        report_version=meta.version or "",
    )
    use_model = runtime_settings.get_default_embedding_model()
    entity_id = ent_repo.upsert_entity(
        conn,
        name=tentative,
        synonym=meta.config_synonym or "",
        version=meta.version or "",
        file_path=str(resolved),
        model=use_model,
        entity_type=meta.entity_type or "configuration",
        name_locked=bool((name or "").strip()),
        source_mode="dump",
        source_location="path",
        source_path=str(resolved),
        ingest_profile=profile,
        dumps_dir=str(resolved),
        zip_path="",
        comment=comment,
        bsl_embed_mode=str(profile.get("bsl_embed_mode") or ""),
        embed_window_preset=str(profile.get("embed_window_preset") or ""),
    )
    return int(entity_id)


def try_post_dump_ingest(
    conn: sqlite3.Connection,
    profile_row: sqlite3.Row | dict,
    *,
    out_dir: str | Path,
    submit_parse: bool = True,
) -> PostIngestResult:
    """Run post-step after successful dump. Caller must skip when ingest_state=blocked."""
    from fastapi import HTTPException

    from app.services import dump_store

    profile_id = int(
        profile_row["id"] if isinstance(profile_row, sqlite3.Row) else profile_row["id"]
    )
    entity_id = profile_row["entity_id"] if isinstance(profile_row, sqlite3.Row) else profile_row.get("entity_id")
    post_action = (
        profile_row["post_action"] if isinstance(profile_row, sqlite3.Row) else profile_row.get("post_action")
    ) or ""
    pa = post_action.strip().lower()

    if pa == "create_entity" and not entity_id:
        try:
            profile_name = (
                profile_row["name"] if isinstance(profile_row, sqlite3.Row) else profile_row.get("name")
            ) or ""
            comment = (
                profile_row["comment"] if isinstance(profile_row, sqlite3.Row) else profile_row.get("comment")
            ) or ""
            resolved = Path(out_dir).resolve()
            meta = read_configuration_meta(resolved)
            tentative = (
                profile_name.strip()
                or (meta.config_name or "").strip()
                or resolved.name
            )
            name = ent_repo.suggest_unique_name(conn, tentative)
            new_id = create_entity_from_dump_dir(
                conn,
                out_dir=out_dir,
                name=name,
                comment=comment or None,
            )
            dump_store.update_profile(
                conn,
                profile_id,
                {"entity_id": new_id, "post_action": "import"},
            )
            if submit_parse:
                jobs.submit(parse_entity, new_id)
            return PostIngestResult(INGEST_DONE, "")
        except HTTPException as exc:
            detail = exc.detail
            msg = (
                detail.get("message", str(detail))
                if isinstance(detail, dict)
                else str(detail)
            )
            log.warning("post-dump create_entity blocked profile=%s: %s", profile_id, msg)
            return PostIngestResult("failed", msg)
        except Exception as exc:
            log.exception("post-dump create_entity failed profile=%s", profile_id)
            return PostIngestResult("failed", str(exc))

    if not entity_id:
        return PostIngestResult(INGEST_PENDING, "")
    if not post_action_triggers_ingest(post_action):
        return PostIngestResult(INGEST_PENDING, "")

    row = ent_repo.get_entity(conn, int(entity_id))
    if not row:
        return PostIngestResult("failed", "привязанная сущность не найдена")

    status = str(row.get("status") or "")
    if status in ENTITY_BUSY_STATUSES:
        return PostIngestResult(INGEST_PENDING, DEFERRED_MSG)

    try:
        import_dump_dir_to_entity(conn, out_dir=out_dir, entity_id=int(entity_id))
        if submit_parse:
            jobs.submit(parse_entity, int(entity_id))
        return PostIngestResult(INGEST_DONE, "")
    except Exception as exc:
        log.exception("post-dump ingest failed entity=%s", entity_id)
        return PostIngestResult("failed", str(exc))
