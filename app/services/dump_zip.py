"""Безопасная распаковка dump-архива (этап 3, ТЗ §5.3/§13).

- Защита от path traversal: только относительные пути, без ``..``, без
  абсолютных путей; результат нормализуется и обязан остаться внутри
  ``dumps/e{id}/``.
- Защита от zip bomb: лимиты из Settings (entries, суммарный несжатый размер,
  размер одного файла, compression ratio); превышение → ``ZipSafetyError``.
- Извлекаются ТОЛЬКО файлы allowlist по профилю (§5.3): метаданные-объекты
  ``*.xml`` (кроме ``**/Help/**``), ``*.bsl`` при ``bsl``, ``**/Ext/Form.bin`` при
  ``ordinary_forms``.
  Бинарники картинок, ``Template.bin``, прочие тяжёлые файлы — пропускаются.
  Каталог ``CommonPictures/**`` целиком НЕ исключается (только бинарники в нём).
- Имена в zip: UTF-8 (флаг zipfile) / cp866 / cp437 — с декодировкой cp866,
  если это не UTF-8 и байты выглядят как cp866.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo

from app.core.config import settings
from app.services.dump_assets_parser import (
    is_managed_form_xml_rel,
    is_template_ext_xml_rel,
)
from app.services.path_nfc import nfc

log = logging.getLogger("cfsmcp2.dump_zip")

# Расширения-бинари картинок и прочие тяжёлые форматы, которые не нужны
# ни одному профилю (этап 3 извлекает только metadata/bsl файлы).
_IMAGE_EXTS = frozenset(
    {".png", ".svg", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tiff", ".webp"}
)
_OTHER_HEAVY_EXTS = frozenset(
    {
        ".zip", ".rar", ".7z", ".gz", ".tgz", ".bz2", ".xz",
        ".exe", ".dll", ".jar", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
        ".mxl", ".epf", ".erf", ".cfe", ".cf", ".dt", ".ddt",
    }
)

# ``**/Help/**`` — справочная информация; извлекаются только HTML-файлы
# (``*.html``/``*.htm``) при профиле ``help`` (по умолчанию включён).
_HELP_SEGMENT = "Help"


class ZipSafetyError(Exception):
    """Превышение лимита безопасности или небезопасный путь в zip."""


@dataclass
class ExtractStats:
    total_entries: int = 0
    extracted: int = 0
    skipped: int = 0
    rejected: int = 0
    errors: int = 0


def _decode_zip_name(info: ZipInfo) -> str:
    """Декодирует имя entry с учётом cp866/cp437/UTF-8.

    Python распаковывает имя как UTF-8 при выставленном флаге bit 11, иначе —
    как cp437. Для архивов, созданных в Windows (7-Zip/пр.) реальная кодировка
    имени обычно cp866 — пробуем перекодировать cp437→cp866.
    """
    if info.flag_bits & 0x800:  # UTF-8 flag
        return nfc(info.filename)
    try:
        raw = info.filename.encode("cp437")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return nfc(info.filename)
    for enc in ("cp866", "utf-8"):
        try:
            dec = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if any(ord(ch) > 0x2FFF for ch in dec):
            return nfc(dec)
    return nfc(info.filename)


def _has_help_segment(rel: PurePosixPath) -> bool:
    return any(p == _HELP_SEGMENT for p in rel.parts)


def _profile_flag(profile: dict, key: str, default: bool = False) -> bool:
    v = profile.get(key)
    if isinstance(v, bool):
        return v
    return default


def _is_extractable(name: str, profile: dict) -> bool:
    """Allowlist-решение для одного файла (ТЗ §5.3, этап 3)."""
    rel = PurePosixPath(name)
    if rel.is_absolute() or ".." in rel.parts:
        return False
    lower = name.lower()
    is_help_html = _has_help_segment(rel) and lower.endswith((".html", ".htm"))
    if _has_help_segment(rel) and not (
        is_help_html and _profile_flag(profile, "help", True)
    ):
        # Справка (U10): извлекаем только HTML при профиле help; всё остальное
        # под ``**/Help/**`` (бинарники, xml, …) по-прежнему пропускаем.
        return False
    if any(p.lower().startswith(("__macosx", ".git", ".svn", ".hg")) for p in rel.parts):
        return False
    if lower.endswith("/"):  # каталог
        return False
    ext = Path(lower).suffix
    if ext == ".xml":
        rel_posix = PurePosixPath(name.replace("\\", "/"))
        if is_managed_form_xml_rel(rel_posix):
            return _profile_flag(profile, "managed_forms")
        if is_template_ext_xml_rel(rel_posix):
            return _profile_flag(profile, "dcs") or _profile_flag(
                profile, "spreadsheet_templates"
            )
        return True
    if ext in _IMAGE_EXTS or ext in _OTHER_HEAVY_EXTS:
        return False
    if ext == ".bin":
        # Обычные (не управляемые) формы — Form.bin; отдельный флаг ordinary_forms.
        return _profile_flag(profile, "ordinary_forms") and lower.endswith(
            "/ext/form.bin"
        )
    if ext == ".bsl":
        return _profile_flag(profile, "bsl", True)
    if ext in {".txt", ".md", ".json", ".log"}:
        return False
    if ext in {".html", ".htm"}:
        # HTML вне ``**/Help/**`` не является справкой — пропускаем.
        return is_help_html
    return False


def _normalized_rel(rel: PurePosixPath) -> PurePosixPath | None:
    """Проверка безопасности пути; возвращает нормализованный относительный путь."""
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        return None
    return rel


def _entry_limit(
    file_size: int,
    compress_size: int,
    *,
    max_file_bytes: int | None = None,
    max_compression_ratio: int | None = None,
) -> None:
    max_file = int(max_file_bytes if max_file_bytes is not None else settings.zip_max_file_bytes)
    if max_file > 0 and file_size > max_file:
        raise ZipSafetyError(
            f"zip entry слишком большой: {file_size} байт > лимит {max_file}"
        )
    max_ratio = int(
        max_compression_ratio
        if max_compression_ratio is not None
        else settings.zip_max_compression_ratio
    )
    if max_ratio > 0 and compress_size > 0:
        ratio = file_size / compress_size
        if ratio > max_ratio:
            raise ZipSafetyError(
                f"zip entry подозрительно сжат: ratio {ratio:.1f} > {max_ratio}"
            )


def extract_dump_zip(
    zip_path: Path,
    dest_dir: Path,
    profile: dict,
    *,
    max_entries: int | None = None,
    max_uncompressed: int | None = None,
    max_file_bytes: int | None = None,
    max_compression_ratio: int | None = None,
) -> ExtractStats:
    """Распаковывает allowlist-файлы zip в ``dest_dir`` (безопасно).

    Каждый разрешённый файл пишется на диск по нормализованному относительному
    пути внутри ``dest_dir``. Возвращает статистику. Кидает ``ZipSafetyError``
    при превышении лимитов безопасности. Параметры ``max_*`` позволяют переопределить
    лимиты из Settings (используется в тестах).
    """
    max_entries = int(max_entries if max_entries is not None else settings.zip_max_entries)
    max_uncompressed = int(
        max_uncompressed
        if max_uncompressed is not None
        else settings.zip_max_uncompressed_bytes
    )

    dest_dir = dest_dir.resolve()
    stats = ExtractStats()
    total_uncompressed = 0

    with ZipFile(zip_path) as zf:
        names = zf.namelist()
        if max_entries > 0 and len(names) > max_entries:
            raise ZipSafetyError(
                f"zip содержит {len(names)} entries > лимит {max_entries}"
            )

        for info in zf.infolist():
            stats.total_entries += 1
            name = _decode_zip_name(info)
            rel = _normalized_rel(PurePosixPath(name.replace("\\", "/")))
            if rel is None:
                stats.rejected += 1
                log.warning("zip: небезопасный путь, пропущен: %r", name)
                continue
            if not _is_extractable(name, profile):
                stats.skipped += 1
                continue

            size = int(info.file_size or 0)
            if max_uncompressed > 0:
                total_uncompressed += size
                if total_uncompressed > max_uncompressed:
                    raise ZipSafetyError(
                        f"zip суммарный распакованный размер {total_uncompressed} "
                        f"> лимит {max_uncompressed}"
                    )
            _entry_limit(
                size,
                int(info.compress_size or 0),
                max_file_bytes=max_file_bytes,
                max_compression_ratio=max_compression_ratio,
            )

            target = dest_dir.joinpath(*rel.parts)
            if not target.is_relative_to(dest_dir):
                stats.rejected += 1
                log.warning("zip: путь выходит за dest_dir, пропущен: %r", name)
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                stats.extracted += 1
            except OSError:
                stats.errors += 1
                log.exception("zip: не удалось извлечь %s", name)

    log.info(
        "dump zip %s: entries=%s extracted=%s skipped=%s rejected=%s errors=%s",
        zip_path.name,
        stats.total_entries,
        stats.extracted,
        stats.skipped,
        stats.rejected,
        stats.errors,
    )
    return stats


def find_configuration_xml(dumps_dir: Path) -> Path | None:
    """Ищет ``Configuration.xml`` в корне или на один уровень вложенности."""
    root = resolve_dump_root(dumps_dir)
    p = root / "Configuration.xml"
    return p if p.is_file() else None


def resolve_dump_root(dumps_dir: Path) -> Path:
    """Каталог Hierarchical-выгрузки (где лежит ``Configuration.xml``).

    Браузерный zip часто содержит обёртку ``ИмяПапки/Configuration.xml`` —
    тогда возвращаем вложенный каталог. При неоднозначности оставляем
    ``dumps_dir`` как есть.
    """
    dumps_dir = Path(dumps_dir)
    if not dumps_dir.is_dir():
        return dumps_dir
    direct = dumps_dir / "Configuration.xml"
    if direct.is_file():
        return dumps_dir
    candidates: list[Path] = []
    try:
        children = list(dumps_dir.iterdir())
    except OSError:
        return dumps_dir
    for child in children:
        if child.is_dir() and (child / "Configuration.xml").is_file():
            candidates.append(child)
    if len(candidates) == 1:
        return candidates[0]
    # Не глубже одного уровня — иначе риск ложного срабатывания
    return dumps_dir
