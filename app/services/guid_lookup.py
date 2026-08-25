"""GUID / UUID helpers for dump metadata identity (find_by_guid)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from app.services.kinds import KIND_RU_TO_EN

# EN singular (ConfigDumpInfo / MetaDataObject) → RU plural path root.
KIND_EN_TO_RU_PLURAL: dict[str, str] = {}
for _ru, _en in KIND_RU_TO_EN.items():
    KIND_EN_TO_RU_PLURAL.setdefault(_en, _ru)

_CHILD_EN_TO_SEG: dict[str, str] = {
    "Attribute": "Реквизиты",
    "TabularSection": "ТабличныеЧасти",
    "Dimension": "Измерения",
    "Resource": "Ресурсы",
    "EnumValue": "ЗначенияПеречисления",
    "Form": "Формы",
    "Template": "Макеты",
    "Command": "Команды",
    "AccountingFlag": "ПризнакиУчета",
    "ExtDimensionAccountingFlag": "ПризнакиУчетаСубконто",
}

_MODULE_SUFFIXES = frozenset(
    {
        "ObjectModule",
        "ManagerModule",
        "RecordSetModule",
        "RecordManagerModule",
        "CommandModule",
        "ValueManagerModule",
        "Module",
    }
)

_GUID_RE = re.compile(
    r"^\{?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\}?"
    r"(?:\.(\d+))?$"
)


def normalize_guid(raw: str) -> tuple[str, str]:
    """Return ``(base_guid_lower, full_or_base)``.

    Accepts braces and optional ``.N`` module suffix (ConfigDumpInfo ObjectModule).
    Empty / invalid → ``("", "")``.
    """
    s = (raw or "").strip()
    if not s:
        return "", ""
    m = _GUID_RE.match(s)
    if not m:
        # Loose: strip braces and lowercase if looks like uuid
        t = s.strip("{}").strip().lower()
        if len(t) >= 36 and t[8] == "-":
            base = t[:36]
            return base, t
        return "", ""
    base = m.group(1).lower()
    suffix = m.group(2)
    full = f"{base}.{suffix}" if suffix is not None else base
    return base, full


def elem_uuid(elem) -> str:
    """``uuid`` attribute on a MetaDataObject child (Catalog / Attribute / …)."""
    raw = ""
    try:
        raw = str(elem.attrib.get("uuid") or "")
    except (AttributeError, TypeError):
        return ""
    base, _full = normalize_guid(raw)
    return base


def dumpinfo_name_to_ru_path(meta_name: str) -> dict[str, Any]:
    """Map ConfigDumpInfo ``Metadata/@name`` to RU object path (+ module hint)."""
    name = (meta_name or "").strip()
    out: dict[str, Any] = {"dump_name": name}
    parts = name.split(".")
    if len(parts) < 2:
        return out
    en_kind, obj_name, *rest = parts
    ru = KIND_EN_TO_RU_PLURAL.get(en_kind)
    if not ru:
        out["en_kind"] = en_kind
        return out
    path = f"{ru}.{obj_name}"
    i = 0
    while i < len(rest):
        seg = rest[i]
        if seg in _MODULE_SUFFIXES:
            out["path"] = path
            out["module_role"] = seg
            out["kind_hint"] = "Module"
            return out
        if seg in _CHILD_EN_TO_SEG and i + 1 < len(rest):
            path = f"{path}.{_CHILD_EN_TO_SEG[seg]}.{rest[i + 1]}"
            i += 2
            continue
        out["path"] = path
        out["tail"] = rest[i:]
        return out
    out["path"] = path
    return out


def find_in_config_dump_info(
    dumps_dir: Path,
    guid: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Scan ``ConfigDumpInfo.xml`` for ``id`` matching guid (base or full).

    Returns list of {guid, dump_name, path?, module_role?, …}.
    Stops after ``limit`` hits (ConfigDumpInfo can be huge).
    """
    base, full = normalize_guid(guid)
    if not base:
        return []
    info = Path(dumps_dir) / "ConfigDumpInfo.xml"
    if not info.is_file():
        return []
    lim = max(1, min(int(limit), 50))
    # Exact id when caller passed .N suffix; otherwise match base and any .N variants.
    if full != base:
        want_exact = {full}
        want_base: set[str] = set()
    else:
        want_exact = {base}
        want_base = {base}
    hits: list[dict[str, Any]] = []
    try:
        for _event, elem in ET.iterparse(str(info), events=("end",)):
            if not str(elem.tag).endswith("Metadata"):
                elem.clear()
                continue
            mid = str(elem.attrib.get("id") or "").strip().lower()
            if not mid:
                elem.clear()
                continue
            mid_base = mid.split(".", 1)[0]
            if mid not in want_exact and mid_base not in want_base:
                elem.clear()
                continue
            dump_name = str(elem.attrib.get("name") or "")
            mapped = dumpinfo_name_to_ru_path(dump_name)
            hits.append(
                {
                    "guid": mid,
                    "dump_name": dump_name,
                    "path": mapped.get("path") or "",
                    "module_role": mapped.get("module_role") or "",
                    "kind_hint": mapped.get("kind_hint") or "",
                    "source": "ConfigDumpInfo.xml",
                }
            )
            elem.clear()
            if len(hits) >= lim:
                break
    except ET.ParseError:
        return hits
    return hits
