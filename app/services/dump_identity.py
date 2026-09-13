"""Post-dump identity check before auto-ingest (design decision 21, S1 finding 9d)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services.dump_parser import read_configuration_meta

INGEST_PENDING = "pending"
INGEST_BLOCKED = "blocked"
INGEST_SKIPPED = "skipped"
INGEST_DONE = "done"


@dataclass(frozen=True, slots=True)
class IngestVerdict:
    ingest_state: str
    message: str = ""


def evaluate_ingest_verdict(
    *,
    target_type: str,
    extension_name: str,
    out_dir: str | Path,
    entity_name: str = "",
    entity_type: str = "",
    dump_succeeded: bool,
) -> IngestVerdict:
    if not dump_succeeded:
        return IngestVerdict("", "")
    try:
        meta = read_configuration_meta(Path(out_dir))
    except Exception as exc:
        return IngestVerdict(INGEST_BLOCKED, f"не удалось прочитать Configuration.xml: {exc}")

    messages: list[str] = []
    if target_type == "extension":
        expected = extension_name.strip()
        actual = meta.config_name.strip()
        if expected and actual and expected.lower() != actual.lower():
            messages.append(
                f"в каталоге выгрузка «{actual}», профиль ожидает расширение «{expected}»"
            )
        elif meta.entity_type == "extension" and not expected:
            pass
        elif meta.entity_type != "extension":
            messages.append("в каталоге выгрузка конфигурации, профиль — расширение")
    elif meta.entity_type == "extension":
        messages.append(
            f"в каталоге выгрузка расширения «{meta.config_name}», профиль — конфигурация"
        )

    if entity_name.strip():
        if meta.config_name.strip().lower() != entity_name.strip().lower():
            messages.append(
                f"в каталоге выгрузка «{meta.config_name}», контекст ожидает «{entity_name}»"
            )
    if entity_type.strip() and meta.entity_type != entity_type.strip():
        messages.append(
            f"тип выгрузки {meta.entity_type}, контекст ожидает {entity_type.strip()}"
        )

    if messages:
        return IngestVerdict(INGEST_BLOCKED, "; ".join(messages))
    return IngestVerdict(INGEST_PENDING, "")
