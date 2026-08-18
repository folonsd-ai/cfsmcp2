"""Парсер форм, СКД и XML-макетов из Hierarchical-выгрузки (этап 6, ТЗ §7.2 п.6).

- ``**/Forms/*/Ext/Form.xml`` → ``ManagedForm``, путь ``….Формы.<Имя>`` /
  ``ОбщиеФормы.<Имя>``.
- ``**/Templates/*/Ext/Template.xml`` с корнем ``DataCompositionSchema`` →
  ``DataCompositionSchema``; иначе → ``SpreadsheetTemplate``.
- Классификация Template.xml — по корневому элементу (peek / streaming).
- props_json: метаданные + сжатое дерево / структура СКД для ``get_skd``.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from app.services.dump_parser import _dump_record, _local, _synonym_best
from app.services.kinds import CONTAINER_EN_TO_RU
from app.services.path_nfc import nfc_rel

log = logging.getLogger("cfsmcp2.dump_assets")

_FORM_ELEMENT_TAGS = frozenset(
    {
        "InputField",
        "LabelField",
        "Button",
        "CheckBoxField",
        "RadioButtonField",
        "Table",
        "UsualGroup",
        "Pages",
        "Page",
        "CommandBar",
        "ButtonGroup",
        "SpreadSheetDocumentField",
        "PictureField",
        "CalendarField",
        "ChartField",
        "GanttChartField",
        "DendrogramField",
        "GraphicalSchemaField",
        "HTMLDocumentField",
        "PlannerField",
        "FormattedDocumentField",
        "TextDocumentField",
        "GeographicalSchemaField",
    }
)

_MAX_FORM_ELEMENTS = 120
_MAX_FORM_EVENTS = 40
_MAX_FORM_ATTRS = 60


def _profile_flag(profile: dict, key: str, default: bool = False) -> bool:
    v = profile.get(key)
    return v if isinstance(v, bool) else default


def is_managed_form_xml_rel(rel: PurePosixPath) -> bool:
    parts = [p.lower() for p in rel.parts]
    if len(parts) < 4 or parts[-1] != "form.xml" or parts[-2] != "ext":
        return False
    if parts[0] == "commonforms" and len(parts) == 4:
        return True
    return len(parts) >= 6 and parts[-4] == "forms"


def is_template_ext_xml_rel(rel: PurePosixPath) -> bool:
    parts = [p.lower() for p in rel.parts]
    if len(parts) < 4 or parts[-1] != "template.xml" or parts[-2] != "ext":
        return False
    if parts[0] == "commontemplates" and len(parts) == 4:
        return True
    return len(parts) >= 6 and parts[-4] == "templates"


def peek_xml_root_local_name(path: Path, *, read_bytes: int = 8192) -> str:
    """Лёгкий peek корневого элемента XML (без полного parse)."""
    try:
        with path.open("rb") as f:
            chunk = f.read(read_bytes)
    except OSError:
        return ""
    text = chunk.decode("utf-8", errors="replace").lstrip("\ufeff")
    m = re.search(r"<([A-Za-z_][\w.-]*(?::[\w.-]+)?)", text)
    if not m:
        return ""
    tag = m.group(1)
    return tag.rsplit(":", 1)[-1] if ":" in tag else tag


def resolve_form_object_path(rel_path: str) -> str | None:
    p = PurePosixPath(nfc_rel(rel_path))
    parts = p.parts
    if not parts:
        return None
    if parts[0] == "CommonForms" and len(parts) >= 4:
        return f"ОбщиеФормы.{parts[1]}"
    if len(parts) >= 6 and parts[-4] == "Forms":
        ru = CONTAINER_EN_TO_RU.get(parts[0])
        if not ru:
            return None
        return f"{ru}.{parts[1]}.Формы.{parts[-3]}"
    return None


def resolve_template_object_path(rel_path: str) -> str | None:
    p = PurePosixPath(nfc_rel(rel_path))
    parts = p.parts
    if not parts:
        return None
    if parts[0] == "CommonTemplates" and len(parts) >= 4:
        return f"ОбщиеМакеты.{parts[1]}"
    if len(parts) >= 6 and parts[-4] == "Templates":
        ru = CONTAINER_EN_TO_RU.get(parts[0])
        if not ru:
            return None
        return f"{ru}.{parts[1]}.Макеты.{parts[-3]}"
    return None


def _text_el(el) -> str:
    return (el.text or "").strip()


def _child_text(el, tag_local: str) -> str:
    for c in el:
        if _local(c.tag) == tag_local:
            return _text_el(c)
    return ""


def _title_from_el(el) -> str:
    for c in el:
        if _local(c.tag) == "title":
            return _synonym_best(c)
    return ""


def _summarize_form(xml_path: Path) -> dict[str, Any]:
    """Streaming summary формы — без полного дерева в RAM."""
    props: dict[str, Any] = {
        "element_count": 0,
        "element_types": {},
        "elements": [],
        "events": [],
        "attributes": [],
        "commands": [],
    }
    in_attributes = False
    in_commands = False
    try:
        for event, elem in ET.iterparse(str(xml_path), events=("start", "end")):
            tag = _local(elem.tag)
            if event == "start":
                if tag == "Attributes":
                    in_attributes = True
                elif tag == "Commands":
                    in_commands = True
                continue
            # end
            if tag == "Attributes":
                in_attributes = False
                elem.clear()
                continue
            if tag == "Commands":
                in_commands = False
                elem.clear()
                continue
            if tag == "Event" and len(props["events"]) < _MAX_FORM_EVENTS:
                nm = _child_text(elem, "name") or elem.attrib.get("name", "")
                handler = _child_text(elem, "handler") or elem.attrib.get("handler", "")
                if nm:
                    props["events"].append({"name": nm, "handler": handler})
            elif in_attributes and tag == "Attribute" and len(props["attributes"]) < _MAX_FORM_ATTRS:
                nm = _child_text(elem, "name") or elem.attrib.get("name", "")
                if nm:
                    props["attributes"].append(nm)
            elif in_commands and tag == "Command" and len(props["commands"]) < _MAX_FORM_ATTRS:
                nm = _child_text(elem, "name") or elem.attrib.get("name", "")
                if nm:
                    props["commands"].append(nm)
            elif tag in _FORM_ELEMENT_TAGS:
                props["element_count"] += 1
                props["element_types"][tag] = props["element_types"].get(tag, 0) + 1
                if len(props["elements"]) < _MAX_FORM_ELEMENTS:
                    nm = _child_text(elem, "name") or elem.attrib.get("name", "")
                    props["elements"].append({"name": nm, "type": tag})
            elif tag in ("WindowOpeningMode", "UseForFoldersAndItems", "AutoTitle"):
                props[tag] = _text_el(elem)
            if tag in ("Attributes", "Commands", "ChildItems", "Form"):
                elem.clear()
    except ET.ParseError as exc:
        log.debug("form parse failed %s: %s", xml_path, exc)
        props["parse_error"] = str(exc)[:120]
    return props


def parse_form_file(xml_path: Path, rel_path: str) -> dict[str, Any] | None:
    obj_path = resolve_form_object_path(rel_path)
    if not obj_path:
        return None
    name = obj_path.rsplit(".", 1)[-1]
    summary = _summarize_form(xml_path)
    props = {
        "Name": name,
        "Synonym": summary.pop("Synonym", "") if "Synonym" in summary else "",
        "_source_rel": rel_path.replace("\\", "/"),
        **summary,
    }
    rec = _dump_record(obj_path, "Форма", "ManagedForm", props)
    rec["source_rel"] = rel_path.replace("\\", "/")
    return rec


def _parse_dcs_structure(xml_path: Path) -> dict[str, Any]:
    """Структура СКД для props / get_skd (streaming iterparse)."""
    data_sets: list[dict[str, Any]] = []
    fields_flat: list[dict[str, str]] = []
    parameters: list[dict[str, str]] = []
    settings_variants: list[dict[str, str]] = []
    current_ds: dict[str, Any] | None = None
    in_data_set = False

    try:
        for event, elem in ET.iterparse(str(xml_path), events=("start", "end")):
            tag = _local(elem.tag)
            if event == "start":
                if tag == "dataSet":
                    in_data_set = True
                    current_ds = {"name": "", "type": "", "fields": []}
                continue
            # end — не вызываем elem.clear() на вложенных узлах до закрытия dataSet
            if tag == "dataSet" and current_ds is not None:
                current_ds["name"] = _child_text(elem, "name")
                xsi_type = elem.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
                current_ds["type"] = xsi_type.rsplit(":", 1)[-1] if xsi_type else ""
                data_sets.append(current_ds)
                current_ds = None
                in_data_set = False
                elem.clear()
            elif tag == "field" and in_data_set and current_ds is not None:
                fp = {
                    "data_path": _child_text(elem, "dataPath"),
                    "field": _child_text(elem, "field"),
                    "title": _title_from_el(elem),
                }
                if fp["data_path"] or fp["field"]:
                    current_ds["fields"].append(fp)
                    fields_flat.append({**fp, "data_set": current_ds.get("name") or ""})
            elif tag == "parameter":
                nm = _child_text(elem, "name")
                if nm:
                    parameters.append({"name": nm, "title": _title_from_el(elem)})
                elem.clear()
            elif tag == "settingsVariant":
                nm = _child_text(elem, "name")
                pres = _child_text(elem, "presentation")
                if nm or pres:
                    settings_variants.append({"name": nm, "presentation": pres})
                elem.clear()
    except ET.ParseError as exc:
        log.debug("dcs parse failed %s: %s", xml_path, exc)
        return {"parse_error": str(exc)[:120]}

    # dedupe flat fields by data_path+field
    seen: set[tuple[str, str]] = set()
    unique_fields: list[dict[str, str]] = []
    for f in fields_flat:
        key = (f.get("data_path") or "", f.get("field") or "")
        if key in seen:
            continue
        seen.add(key)
        unique_fields.append(f)

    return {
        "data_sets": data_sets,
        "fields": unique_fields,
        "parameters": parameters,
        "settings_variants": settings_variants,
    }


def parse_dcs_file(xml_path: Path, rel_path: str) -> dict[str, Any] | None:
    obj_path = resolve_template_object_path(rel_path)
    if not obj_path:
        return None
    name = obj_path.rsplit(".", 1)[-1]
    skd = _parse_dcs_structure(xml_path)
    props = {
        "Name": name,
        "Synonym": "",
        "_source_rel": rel_path.replace("\\", "/"),
        "skd": skd,
    }
    rec = _dump_record(obj_path, "СКД", "DataCompositionSchema", props)
    rec["source_rel"] = rel_path.replace("\\", "/")
    return rec


def _summarize_spreadsheet(xml_path: Path) -> dict[str, Any]:
    props: dict[str, Any] = {"rows": 0, "columns": 0, "parameters": []}
    try:
        for event, elem in ET.iterparse(str(xml_path), events=("end",)):
            tag = _local(elem.tag)
            if tag == "rowsItem":
                props["rows"] += 1
            elif tag == "columns":
                sz = _child_text(elem, "size")
                if sz.isdigit():
                    props["columns"] = max(props["columns"], int(sz))
            elif tag == "parameter" and len(props["parameters"]) < 40:
                nm = _child_text(elem, "name") or elem.attrib.get("name", "")
                if nm:
                    props["parameters"].append(nm)
            elem.clear()
    except ET.ParseError as exc:
        props["parse_error"] = str(exc)[:120]
    return props


def parse_spreadsheet_file(xml_path: Path, rel_path: str) -> dict[str, Any] | None:
    obj_path = resolve_template_object_path(rel_path)
    if not obj_path:
        return None
    name = obj_path.rsplit(".", 1)[-1]
    summary = _summarize_spreadsheet(xml_path)
    props = {
        "Name": name,
        "Synonym": "",
        "_source_rel": rel_path.replace("\\", "/"),
        **summary,
    }
    rec = _dump_record(obj_path, "Макет", "SpreadsheetTemplate", props)
    rec["source_rel"] = rel_path.replace("\\", "/")
    return rec


def classify_template_file(xml_path: Path) -> str | None:
    """``dcs`` | ``spreadsheet`` | None."""
    root = peek_xml_root_local_name(xml_path)
    if root == "DataCompositionSchema":
        return "dcs"
    if root in ("document", "Document"):
        return "spreadsheet"
    if root:
        return "spreadsheet"
    return None


def iter_dump_asset_records(
    dumps_dir: Path,
    profile: dict,
    should_parse: Callable[[Path], bool] | None = None,
    *,
    form_files: list[Path] | None = None,
    template_files: list[Path] | None = None,
) -> Iterator[dict[str, Any]]:
    """Обход Form.xml / Template.xml по флагам профиля."""
    want_forms = _profile_flag(profile, "managed_forms")
    want_dcs = _profile_flag(profile, "dcs")
    want_tpl = _profile_flag(profile, "spreadsheet_templates")
    if not (want_forms or want_dcs or want_tpl):
        return

    seen: set[str] = set()

    if want_forms:
        form_iter = (
            form_files
            if form_files is not None
            else sorted(dumps_dir.rglob("Form.xml"))
        )
        for xml_path in form_iter:
            if not xml_path.is_file():
                continue
            rel = nfc_rel(xml_path.relative_to(dumps_dir).as_posix())
            rel_p = PurePosixPath(rel)
            if form_files is None and not is_managed_form_xml_rel(rel_p):
                continue
            if rel in seen:
                continue
            seen.add(rel)
            if should_parse is not None and not should_parse(xml_path):
                continue
            try:
                rec = parse_form_file(xml_path, rel)
                if rec:
                    yield rec
            except Exception:
                log.exception("form asset failed: %s", rel)

    if want_dcs or want_tpl:
        tpl_iter = (
            template_files
            if template_files is not None
            else sorted(dumps_dir.rglob("Template.xml"))
        )
        for xml_path in tpl_iter:
            if not xml_path.is_file():
                continue
            rel = nfc_rel(xml_path.relative_to(dumps_dir).as_posix())
            rel_p = PurePosixPath(rel)
            if template_files is None and not is_template_ext_xml_rel(rel_p):
                continue
            if rel in seen:
                continue
            if should_parse is not None and not should_parse(xml_path):
                seen.add(rel)
                continue
            kind = classify_template_file(xml_path)
            if kind == "dcs" and want_dcs:
                seen.add(rel)
                try:
                    rec = parse_dcs_file(xml_path, rel)
                    if rec:
                        yield rec
                except Exception:
                    log.exception("dcs asset failed: %s", rel)
            elif kind == "spreadsheet" and want_tpl:
                seen.add(rel)
                try:
                    rec = parse_spreadsheet_file(xml_path, rel)
                    if rec:
                        yield rec
                except Exception:
                    log.exception("spreadsheet asset failed: %s", rel)


def load_skd_from_file(dumps_dir: Path, source_rel: str) -> dict[str, Any]:
    """Ленивый parse СКД с диска (fallback для get_skd)."""
    rel = nfc_rel(source_rel)
    p = dumps_dir / rel.replace("/", "\\") if "\\" in str(dumps_dir) else dumps_dir / rel
    if not p.is_file():
        p = dumps_dir.joinpath(*PurePosixPath(rel).parts)
    if not p.is_file():
        return {}
    return _parse_dcs_structure(p)
