import re
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8559
    metadata_dir: Path = Path("./data/metadata")
    dumps_dir: Path = Path("./data/dumps")
    db_path: Path = Path("./data/cfsmcp2.sqlite3")
    zvec_dir: Path = Path("./data/zvec")
    # Разрешённые корни для source_location=path (import-path, этап 2.5).
    # Пустой список = ограничений нет (логируется warning на каждый import-path).
    # В env PATH_ALLOWED_ROOTS корни разделяются запятой и/или точкой с запятой.
    # Если задан MOUNT_POINTS — он имеет приоритет как whitelist.
    path_allowed_roots: Annotated[list[str], NoDecode] = []

    # Именованные точки подключения (Docker bind-mounts), видные процессу.
    # Формат: id=/mnt/path,id2=/mnt/path2 или id=/mnt/path|Label
    # Пример: erp=/mnt/erp,ut11=/mnt/ut11,dumps=/mnt/dumps
    mount_points: Annotated[list[str], NoDecode] = []

    # auto | docker | native — как показывать path-delivery в UI.
    # auto: детект по /.dockerenv / cgroup.
    runtime_mode: str = "auto"

    # Безопасность распаковки zip (dump-режим, этап 3, ТЗ §13). Превышение
    # любого лимита → 400 с понятным сообщением (защита от zip bomb).
    zip_max_entries: int = 200_000
    zip_max_uncompressed_bytes: int = 20 * 1024**3  # 20 ГБ суммарный распакованный размер
    zip_max_file_bytes: int = 2 * 1024**3  # 2 ГБ на один файл
    zip_max_compression_ratio: int = 2000  # file_size / compressed_size на entry

    lm_studio_url: str = "http://127.0.0.1:1234"
    default_embedding_model: str = "text-embedding-multilingual-e5-small"
    embedding_batch_size: int = 128
    # Cap texts per LM Studio /v1/embeddings call (objects may expand to many passages in chunks mode)
    embedding_max_texts_per_request: int = 256
    embedding_workers: int = 2
    search_default_limit: int = 50
    search_max_limit: int = 200

    @field_validator("path_allowed_roots", "mount_points", mode="before")
    @classmethod
    def _parse_path_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [p.strip() for p in re.split(r"[;,]", v) if p.strip()]
        if isinstance(v, (list, tuple)):
            return [str(p).strip() for p in v if str(p).strip()]
        return []

    @field_validator("runtime_mode", mode="before")
    @classmethod
    def _parse_runtime_mode(cls, v):
        s = str(v or "auto").strip().lower()
        if s not in {"auto", "docker", "native"}:
            return "auto"
        return s


settings = Settings()
