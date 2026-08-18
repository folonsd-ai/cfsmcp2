"""Нормализация ссылок/путей на входе MCP-tools (этап 3, ТЗ §9/§16).

Принимает RU и EN формы рефов/путей и сводит их к каноническим объектным путям
индекса (`Справочники.Номенклатура.Реквизиты.Код`):

- RU: ``СправочникСсылка.Х`` / ``Справочник.Х`` / ``Справочники.Х``
- EN: ``Catalog.Ref.Х`` / ``CatalogRef.Х`` / ``Catalog.Х`` / ``Catalogs.Х``
- вложенность: ``Attributes↔Реквизиты``, ``TableParts↔ТабличныеЧасти``,
  ``EnumValues↔ЗначенияПеречисления``, ``Dimensions↔Измерения``,
  ``Resources↔Ресурсы``, ``Forms↔Формы``, ``Templates↔Макеты``.

``resolve_qualified_name`` возвращает только пути, существующие в objects
сущности. ``link_ref_candidates`` строит канонические RU-рефы
(``СправочникСсылка.Х`` / ``Документ.Х``) для сопоставления с ``links.to_ref``
в find_usages.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from app.repositories import objects as obj_repo
from app.services.kinds import (
    CONTAINER_EN_TO_RU,
    KIND_EN_REF_TO_RU_REF,
    KIND_EN_TO_RU_SINGULAR,
    KIND_RU_TO_EN,
    LINK_RU_SINGULAR_TO_EN,
)
from app.services.path_nfc import nfc

# RU единственное (в т.ч. с суффиксом Ссылка) → RU множественное (канон пути).
_EN_SINGULAR_TO_RU_PLURAL: dict[str, str] = {
    en: ru for ru, en in KIND_RU_TO_EN.items()
}
_RU_SINGULAR_TO_RU_PLURAL: dict[str, str] = {
    ru: _EN_SINGULAR_TO_RU_PLURAL[en]
    for ru, en in LINK_RU_SINGULAR_TO_EN.items()
    if en in _EN_SINGULAR_TO_RU_PLURAL
}
# Дополнения, которых нет в LINK_RU_SINGULAR_TO_EN (из отчёта) / KIND_RU_TO_EN.
_RU_SINGULAR_TO_RU_PLURAL.update(
    {
        "ОбщийРеквизит": "ОбщиеРеквизиты",
        "НумераторДокумента": "НумераторыДокументов",
        "СервисИнтеграции": "СервисыИнтеграции",
    }
)

_REF_SUFFIX = "Ссылка"

# EN-имена сегментов вложенных сущностей (пути) ↔ RU-канон.
SEGMENT_EN_TO_RU: dict[str, str] = {
    "Attributes": "Реквизиты",
    "TableParts": "ТабличныеЧасти",
    "EnumValues": "ЗначенияПеречисления",
    "Dimensions": "Измерения",
    "Resources": "Ресурсы",
    "Forms": "Формы",
    "Templates": "Макеты",
    "Commands": "Команды",
    "Methods": "Методы",
    "CommandInterface": "КомандныйИнтерфейс",
}
SEGMENT_RU_TO_EN: dict[str, str] = {v: k for k, v in SEGMENT_EN_TO_RU.items()}

# EN-формы заголовка → RU множественное (канон пути).
_HEAD_CANDIDATES: dict[str, str] = {}
for _en, _ru_plural in CONTAINER_EN_TO_RU.items():
    _HEAD_CANDIDATES[_en] = _ru_plural
for _en, _ru_singular in KIND_EN_TO_RU_SINGULAR.items():
    _ru_plural = _RU_SINGULAR_TO_RU_PLURAL.get(_ru_singular, _ru_singular)
    _HEAD_CANDIDATES[_en] = _ru_plural
    _HEAD_CANDIDATES[f"{_en}Ref"] = _ru_plural
    _HEAD_CANDIDATES[f"{_en}.Ref"] = _ru_plural
for _ru_singular, _ru_plural in _RU_SINGULAR_TO_RU_PLURAL.items():
    _HEAD_CANDIDATES[_ru_singular] = _ru_plural
    _HEAD_CANDIDATES[_ru_singular + _REF_SUFFIX] = _ru_plural
    _HEAD_CANDIDATES[_ru_plural] = _ru_plural

# Любая форма head → канонический RU-реф для to_ref:
# - EN-реф (CatalogRef) → RU-реф (СправочникСсылка)
# - EN-singular (Catalog, AccumulationRegister) → RU-singular (Справочник, РегистрНакопления)
# - RU-singular / RU-singular-Ссылка → как есть.
_REF_TO_REF: dict[str, str] = dict(KIND_EN_REF_TO_RU_REF)
for _en, _ru_singular in KIND_EN_TO_RU_SINGULAR.items():
    _REF_TO_REF[_en] = _ru_singular
    _REF_TO_REF[f"{_en}Ref"] = _ru_singular + _REF_SUFFIX
    _REF_TO_REF[f"{_en}.Ref"] = _ru_singular + _REF_SUFFIX
for _ru_singular in _RU_SINGULAR_TO_RU_PLURAL:
    _REF_TO_REF[_ru_singular] = _ru_singular
    _REF_TO_REF[_ru_singular + _REF_SUFFIX] = _ru_singular + _REF_SUFFIX


def _split(ref: str) -> list[str]:
    raw = ref.strip().strip('"')
    if raw.lower().startswith("cfg:"):
        raw = raw[4:]
    return [p for p in raw.split(".") if p]


def _head_and_rest(parts: list[str], table: dict[str, str]) -> tuple[str, list[str]]:
    """Head + rest; учитывает двухсегментный head ``Catalog.Ref`` (и его аналоги)."""
    if len(parts) >= 2:
        two = f"{parts[0]}.{parts[1]}"
        if two in table:
            return two, parts[2:]
    return parts[0], parts[1:]


def normalize_path(ref: str) -> str | None:
    """Реф/путь → канонический объектный путь; None, если head не распознан."""
    if not ref or not isinstance(ref, str):
        return None
    parts = _split(nfc(ref))
    if not parts:
        return None
    head, rest = _head_and_rest(parts, _HEAD_CANDIDATES)
    ru_plural = _HEAD_CANDIDATES.get(head)
    if ru_plural is None:
        return None
    rest = [SEGMENT_EN_TO_RU.get(seg, seg) for seg in rest]
    return ".".join([ru_plural, *rest])


def resolve_qualified_name(
    entity: dict[str, Any],
    ref: str,
    conn: sqlite3.Connection,
) -> list[str]:
    """Кандидаты объектных путей для рефа; только существующие в objects.

    Прямое совпадение пробуем первым (пути BSL-методов и пр. уже каноничны),
    при отсутствии — нормализованный канонический путь.
    """
    if not ref or not isinstance(ref, str):
        return []
    raw = nfc(ref.strip().strip('"'))
    if not raw:
        return []
    existing = obj_repo.lookup_paths(conn, int(entity["id"]), [raw])
    if raw in existing:
        return [raw]
    canonical = normalize_path(raw)
    if not canonical or canonical == raw:
        return []
    found = obj_repo.lookup_paths(conn, int(entity["id"]), [canonical])
    return [canonical] if canonical in found else []


def to_ref_canonical(ref: str) -> str | None:
    """Реф (RU/EN, с/без cfg:) → единственный канонический RU-реф для to_ref.

    Например ``cfg:CatalogRef.Х`` → ``СправочникСсылка.Х``,
    ``AccumulationRegister.Х`` → ``РегистрНакопления.Х``.
    """
    if not ref or not isinstance(ref, str):
        return None
    parts = _split(nfc(ref))
    if not parts:
        return None
    head, rest = _head_and_rest(parts, _REF_TO_REF)
    ru_ref = _REF_TO_REF.get(head)
    if ru_ref is None:
        return None
    rest = [SEGMENT_EN_TO_RU.get(seg, seg) for seg in rest]
    return ".".join([ru_ref, *rest]) if rest else ru_ref


def _ref_heads(head: str) -> set[str]:
    """Канонические RU-реф-формы head (singular и singular-Ссылка)."""
    out: set[str] = set()
    ru_ref = _REF_TO_REF.get(head)
    if ru_ref:
        out.add(ru_ref)
        if ru_ref.endswith(_REF_SUFFIX) and len(ru_ref) > len(_REF_SUFFIX):
            out.add(ru_ref[: -len(_REF_SUFFIX)])
        else:
            out.add(ru_ref + _REF_SUFFIX)
        return out
    ru_plural = _HEAD_CANDIDATES.get(head)
    if ru_plural:
        for ru_s, ru_p in _RU_SINGULAR_TO_RU_PLURAL.items():
            if ru_p == ru_plural:
                out.add(ru_s)
                out.add(ru_s + _REF_SUFFIX)
                break
    return out


def link_ref_candidates(ref: str) -> set[str]:
    """Канонические RU-рефы (singular и singular-Ссылка) для to_ref-материнга.

    Для ``Справочники.Х`` / ``Catalog.Х`` / ``Справочник.Х`` / ``CatalogRef.Х``
    вернёт ``{Справочник.Х, СправочникСсылка.Х}`` (плюс полный канонический путь).
    """
    out: set[str] = set()
    if not ref or not isinstance(ref, str):
        return out
    parts = _split(ref)
    if not parts:
        return out
    head, rest = _head_and_rest(parts, _HEAD_CANDIDATES)
    heads = _ref_heads(head)
    if not heads:
        return out
    rest = [SEGMENT_EN_TO_RU.get(seg, seg) for seg in rest]
    joined = ".".join(rest)
    for h in heads:
        out.add(f"{h}.{joined}" if joined else h)
    canonical = normalize_path(ref)
    if canonical:
        out.add(canonical)
        if "." in canonical:
            head, name = canonical.split(".", 1)
            for ru_s, ru_p in _RU_SINGULAR_TO_RU_PLURAL.items():
                if ru_p == head:
                    out.add(f"{ru_s}.{name}")
                    break
    return out


def iter_paths(conn: sqlite3.Connection, entity_id: int, refs: Iterable[str]) -> set[str]:
    """Канонические пути, существующие в objects, для каждого рефа."""
    out: set[str] = set()
    for ref in refs:
        if not ref or not isinstance(ref, str):
            continue
        p = normalize_path(ref)
        if p and obj_repo.get_object(conn, entity_id, p):
            out.add(p)
    return out
