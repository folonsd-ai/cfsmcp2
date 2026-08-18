"""Парсер Hierarchical-выгрузки 1С (этап 3, ТЗ §7.2).

- ``Configuration.xml`` читается стримингом (iterparse) → имя/синоним/версия/
  entity_type (configuration|extension по ``ConfigurationExtensionPurpose`` /
  ``ObjectBelonging=Adopted|Borrowed``, как в report-детекте; тег
  ``ConfigurationExtensionCompatibilityMode`` НЕ признак CFE — он есть и у CF).
- Объектный XML (``Catalogs/Х.xml``, …): корень ``MetaDataObject`` → узел
  (kind RU plural из имени каталога-контейнера, EN через ``KIND_RU_TO_EN``),
  путь RU (``Справочники.Х``), props = ПОЛНОЕ снятие ``<Properties>``
  (значения строк; типы с квалификаторами ``v8:StringQualifiers`` и пр.;
  составные типы — списком).
- ``ChildObjects`` → вложенные объекты и пути (§6.2): ``Attribute`` →
  ``.Реквизиты.``, ``TabularSection`` → ``.ТабличныеЧасти.`` (+ их реквизиты),
  ``EnumValue`` → ``.ЗначенияПеречисления.``, ``Dimension`` → ``.Измерения.``,
  ``Resource`` → ``.Ресурсы.``. Form/Template — ``dump_assets_parser`` (этап 6).
- Связи: ``Type`` → link_type='type' (нормализация ``cfg:CatalogRef.Х`` →
  ``СправочникСсылка.Х``), ``RegisterRecords`` → 'movements', ``BasedOn`` →
  'based_on', ``Content`` (состав подсистем) → 'composition'.
- CommonPictures: ``CommonPictures/Х.xml`` → объект ``ОбщиеКартинки.Х``.
  Fallback: перечень картинок из ``ConfigDumpInfo.xml``.
- Dump audit: файлы-кандидаты (``*.xml`` в корне контейнера), не породившие
  объектов → записи (пустой, неверный корень, не метаданные).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from app.services.kinds import CONTAINER_EN_TO_RU, KIND_RU_TO_EN, belong_normalize
from app.services.path_nfc import nfc, nfc_rel
from app.services.refs import to_ref_canonical

log = logging.getLogger("cfsmcp2.dump_parser")

MD = "{http://v8.1c.ru/8.3/MDClasses}"

# EN-singular теги объектных элементов внутри MetaDataObject.
_OBJECT_ELEMENT_TAGS = frozenset(
    {
        "Catalog",
        "Document",
        "Enum",
        "Report",
        "DataProcessor",
        "InformationRegister",
        "AccumulationRegister",
        "AccountingRegister",
        "CalculationRegister",
        "ChartOfCharacteristicTypes",
        "ChartOfAccounts",
        "ChartOfCalculationTypes",
        "ExchangePlan",
        "BusinessProcess",
        "Task",
        "DocumentJournal",
        "Constant",
        "CommonModule",
        "CommonForm",
        "CommonCommand",
        "CommonTemplate",
        "CommonPicture",
        "CommandGroup",
        "Role",
        "Subsystem",
        "SessionParameter",
        "Language",
        "DefinedType",
        "FilterCriterion",
        "FunctionalOption",
        "FunctionalOptionsParameter",
        "SettingsStorage",
        "StyleItem",
        "Style",
        "Interface",
        "XDTOPackage",
        "WebService",
        "HTTPService",
        "WSReference",
        "EventSubscription",
        "ScheduledJob",
        "Bot",
        "ExternalDataSource",
        "CommonAttribute",
        "DocumentNumerator",
        "IntegrationService",
    }
)

# ChildObjects → (сегмент пути, kind EN, kind_ru).
_CHILD_MAP: dict[str, tuple[str, str, str]] = {
    "Attribute": ("Реквизиты", "Attribute", "Реквизит"),
    "TabularSection": ("ТабличныеЧасти", "TabularSection", "ТабличнаяЧасть"),
    "Dimension": ("Измерения", "Dimension", "Измерение"),
    "Resource": ("Ресурсы", "Resource", "Ресурс"),
    "EnumValue": ("ЗначенияПеречисления", "EnumValue", "ЗначениеПеречисления"),
}

_LINK_PROPS: dict[str, str] = {
    "RegisterRecords": "movements",
    "BasedOn": "based_on",
    "Content": "composition",
}


@dataclass
class DumpMeta:
    config_name: str
    config_synonym: str
    version: str
    entity_type: str = "configuration"


@dataclass
class AuditEntry:
    path: str
    reason: str
    detail: str = ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ser(el) -> Any:
    """Сериализация элемента в JSON-совместимое значение (строки/списки/dict)."""
    children = list(el)
    if not children:
        return (el.text or "").strip()
    tags = [_local(c.tag) for c in children]
    if all(t == "item" for t in tags):
        return _synonym_best(el)
    if all(t == "Item" for t in tags):
        return [_ser(c) for c in children]
    if len(set(tags)) == 1:
        return [_ser(c) for c in children]
    out: dict[str, Any] = {}
    for c in children:
        t = _local(c.tag)
        v = _ser(c)
        if t in out:
            if not isinstance(out[t], list):
                out[t] = [out[t]]
            out[t].append(v)
        else:
            out[t] = v
    return out


def _synonym_best(el) -> str:
    """v8:item список (lang/content) → строка (ru, иначе первый непустой)."""
    best = ""
    for c in el:
        lang = ""
        content = ""
        for sub in c:
            st = _local(sub.tag)
            txt = (sub.text or "").strip()
            if st == "lang":
                lang = txt
            elif st == "content":
                content = txt
            elif txt and not content:
                content = txt
        if content:
            if lang == "ru":
                return content
            if not best:
                best = content
    return best


def _ser_type(el) -> list[str]:
    """``<Type>`` → список строк: тип + квалификаторы в скобках.

    ``<v8:Type>xs:string</v8:Type>`` + ``<v8:StringQualifiers><v8:Length>0`` →
    ``["xs:string(Length=0,AllowedLength=Variable)", …]``. Составной тип — список.
    """
    out: list[str] = []
    for c in el:
        t = _local(c.tag)
        if t == "Type":
            out.append((c.text or "").strip())
            continue
        quals = [f"{_local(q.tag)}={(q.text or '').strip()}" for q in c]
        suffix = f"({','.join(quals)})" if quals else ""
        if out:
            out[-1] = out[-1] + suffix
        elif suffix:
            out.append(suffix)
    return out


def parse_props(props_el) -> dict[str, Any]:
    """Полное снятие ``<Properties>`` в dict (ключи — локальные имена тегов)."""
    out: dict[str, Any] = {}
    for child in props_el:
        tag = _local(child.tag)
        if tag == "Type":
            out[tag] = _ser_type(child)
        else:
            out[tag] = _ser(child)
    return out


def _dump_record(path: str, kind_ru: str, kind: str, props: dict[str, Any]) -> dict[str, Any]:
    synonym = props.get("Synonym") or ""
    comment = props.get("Comment") or ""
    base_object = props.get("ExtendedConfigurationObject") or ""
    return {
        "path": path,
        "kind_ru": kind_ru,
        "kind": kind,
        "name": path.rsplit(".", 1)[-1],
        "synonym": synonym if isinstance(synonym, str) else "",
        "comment": comment if isinstance(comment, str) else "",
        "belong": belong_normalize(str(props.get("ObjectBelonging") or "") or None),
        "base_object": base_object if isinstance(base_object, str) else "",
        "props": props,
    }


def _iter_scalar_strings(value: Any) -> Iterator[str]:
    """Все строковые скаляры из вложенных значений (dict/list/str)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for v in value:
            yield from _iter_scalar_strings(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_scalar_strings(v)


def extract_dump_links(path: str, props: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Связи из свойств dump-объекта (только структурированные, не строки)."""
    out: list[tuple[str, str, str]] = []

    types = props.get("Type")
    if isinstance(types, str):
        types = [types]
    if isinstance(types, list):
        for t in types:
            t = str(t).strip()
            if not t or "Ref." not in t:
                continue
            canon = to_ref_canonical(t)
            if canon:
                out.append((path, canon, "type"))

    for key, link_type in _LINK_PROPS.items():
        for v in _iter_scalar_strings(props.get(key)):
            v = v.strip()
            if not v:
                continue
            canon = to_ref_canonical(v)
            if canon:
                out.append((path, canon, link_type))
    return out


def _config_name_from_dumpinfo(dumps_dir: Path) -> str:
    p = dumps_dir / "ConfigDumpInfo.xml"
    if not p.is_file():
        return ""
    try:
        for event, elem in ET.iterparse(str(p), events=("end",)):
            if _local(elem.tag) == "Metadata":
                nm = str(elem.attrib.get("name") or "")
                if nm.startswith("Configuration."):
                    rest = nm[len("Configuration.") :]
                    if "." not in rest:
                        return rest
                elem.clear()
    except ET.ParseError:
        log.debug("ConfigDumpInfo.xml parse failed", exc_info=True)
    return ""


def _common_picture_names(dumps_dir: Path) -> list[str]:
    p = dumps_dir / "ConfigDumpInfo.xml"
    names: list[str] = []
    if not p.is_file():
        return names
    try:
        for event, elem in ET.iterparse(str(p), events=("end",)):
            if _local(elem.tag) == "Metadata":
                nm = str(elem.attrib.get("name") or "")
                if nm.startswith("CommonPicture."):
                    rest = nm[len("CommonPicture.") :]
                    if "." not in rest:
                        names.append(rest)
                elem.clear()
    except ET.ParseError:
        log.debug("ConfigDumpInfo.xml parse failed (pictures)", exc_info=True)
    return names


def read_configuration_meta(dumps_dir: Path) -> DumpMeta:
    """Имя/синоним/версия/entity_type из Configuration.xml (streaming).

    CFE-детект как в report (``detect_entity_type``): признак расширения —
    непустой ``ConfigurationExtensionPurpose`` либо ``ObjectBelonging`` корня
    в ``Adopted``/``Borrowed``. ``ConfigurationExtensionCompatibilityMode``
    НЕ учитывается — он есть у любой конфигурации 8.3.6+ (в т.ч. CF).
    """
    cfg_path = dumps_dir / "Configuration.xml"
    name = synonym = version = purpose = belong = ""
    if cfg_path.is_file():
        try:
            for event, elem in ET.iterparse(str(cfg_path), events=("end",)):
                if _local(elem.tag) == "Configuration":
                    props_el = elem.find(f"{MD}Properties")
                    if props_el is not None:
                        props = parse_props(props_el)
                        name = str(props.get("Name") or "")
                        synonym = str(props.get("Synonym") or "")
                        version = str(props.get("Version") or "")
                        purpose = str(props.get("ConfigurationExtensionPurpose") or "")
                        belong = str(props.get("ObjectBelonging") or "")
                    elem.clear()
                    break
        except ET.ParseError:
            log.warning("Configuration.xml parse failed: %s", cfg_path, exc_info=True)
    if not name:
        name = _config_name_from_dumpinfo(dumps_dir)
    is_ext = bool(purpose.strip()) or belong.strip() in ("Adopted", "Borrowed")
    return DumpMeta(
        config_name=name,
        config_synonym=synonym,
        version=version,
        entity_type="extension" if is_ext else "configuration",
    )


def _zip_entry(zf, target: str):
    """ZipInfo для ``target`` в корне или ``*/target`` (одна обёртка-папка)."""
    from app.services.dump_zip import _decode_zip_name

    exact = None
    nested: list = []
    for info in zf.infolist():
        name = _decode_zip_name(info).replace("\\", "/")
        if name == target:
            exact = info
            break
        parts = name.split("/")
        if len(parts) == 2 and parts[1] == target and parts[0] and parts[0] != "..":
            nested.append(info)
    if exact is not None:
        return exact
    if len(nested) == 1:
        return nested[0]
    return None


def read_configuration_meta_from_zip(zip_path: Path) -> DumpMeta:
    """Мета из zip без распаковки: читает Configuration.xml/ConfigDumpInfo.xml."""
    from zipfile import ZipFile

    import tempfile

    if not zip_path.is_file():
        return DumpMeta(config_name="", config_synonym="", version="")
    with ZipFile(zip_path) as zf, tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        for target in ("Configuration.xml", "ConfigDumpInfo.xml"):
            entry = _zip_entry(zf, target)
            if entry is not None:
                try:
                    (tdir / target).write_bytes(zf.read(entry))
                except (OSError, KeyError):
                    log.debug("zip meta read failed for %s", target, exc_info=True)
        return read_configuration_meta(tdir)


class DumpScanner:
    """Стриминговый обход dump: объектные файлы контейнеров + CommonPictures.

    ``iter_records`` отдаёт (record, links); audit-записи копятся в ``audit``.
    """

    def __init__(self, dumps_dir: Path):
        self.dumps_dir = dumps_dir
        self.audit: list[AuditEntry] = []
        self.files_seen = 0
        self.files_ok = 0
        self.files_skipped = 0
        self._picture_paths: set[str] = set()

    def iter_records(
        self,
        should_parse: Callable[[Path], bool] | None = None,
    ) -> Iterator[tuple[dict[str, Any], list[tuple[str, str, str]]]]:
        for container_en, ru_plural in CONTAINER_EN_TO_RU.items():
            cdir = self.dumps_dir / container_en
            if not cdir.is_dir():
                continue
            for xml_file in sorted(cdir.glob("*.xml")):
                self.files_seen += 1
                if should_parse is not None and not should_parse(xml_file):
                    self.files_skipped += 1
                    continue
                rel = nfc_rel(xml_file.relative_to(self.dumps_dir).as_posix())
                try:
                    for rec, links in self._parse_object_file(xml_file, ru_plural, container_en):
                        rec["source_rel"] = rel
                        if container_en == "CommonPictures":
                            self._picture_paths.add(rec["path"])
                        yield rec, links
                    self.files_ok += 1
                except Exception:
                    self.files_ok += 1
                    self.audit.append(
                        AuditEntry(str(xml_file), "parse_error", "unexpected parse exception")
                    )
                    log.exception("dump object file failed: %s", xml_file)
        yield from self._picture_fallback(should_parse)

    def _parse_object_file(
        self, xml_path: Path, ru_plural: str, container_en: str
    ) -> Iterator[tuple[dict[str, Any], list[tuple[str, str, str]]]]:
        obj_elem = None
        try:
            for event, elem in ET.iterparse(str(xml_path), events=("end",)):
                tag = _local(elem.tag)
                if tag in _OBJECT_ELEMENT_TAGS:
                    obj_elem = elem
                    break
        except ET.ParseError as exc:
            self.audit.append(AuditEntry(str(xml_path), "parse_error", str(exc)[:120]))
            return
        if obj_elem is None:
            self.audit.append(
                AuditEntry(str(xml_path), "not_metadata", "нет объектного элемента под MetaDataObject")
            )
            return
        try:
            records, links = self._process_object(obj_elem, ru_plural, container_en)
        finally:
            obj_elem.clear()
        if not records:
            self.audit.append(
                AuditEntry(str(xml_path), "no_name", "объект без имени (Name отсутствует)")
            )
            return
        for rec in records:
            yield rec, links

    def _process_object(
        self, elem, ru_plural: str, container_en: str
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
        tag = _local(elem.tag)
        props_el = elem.find(f"{MD}Properties")
        props = parse_props(props_el) if props_el is not None else {}
        name = nfc(str(props.get("Name") or "").strip())
        if not name:
            return [], []
        path = f"{ru_plural}.{name}"
        kind = KIND_RU_TO_EN.get(ru_plural, ru_plural)
        records = [_dump_record(path, ru_plural, kind, props)]
        links = extract_dump_links(path, props)
        child_records, child_links = self._walk_children(elem, path)
        records.extend(child_records)
        links.extend(child_links)
        return records, links

    def _walk_children(
        self, elem, base_path: str
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
        records: list[dict[str, Any]] = []
        links: list[tuple[str, str, str]] = []
        co = elem.find(f"{MD}ChildObjects")
        if co is None:
            return records, links
        for child in co:
            ctag = _local(child.tag)
            cmap = _CHILD_MAP.get(ctag)
            if cmap is None:
                continue  # Form/Template/Command/… — этап 6
            seg, ckind, ckind_ru = cmap
            cprops_el = child.find(f"{MD}Properties")
            cprops = parse_props(cprops_el) if cprops_el is not None else {}
            cname = nfc(str(cprops.get("Name") or "").strip())
            if not cname:
                continue
            cpath = f"{base_path}.{seg}.{cname}"
            records.append(_dump_record(cpath, ckind_ru, ckind, cprops))
            links.extend(extract_dump_links(cpath, cprops))
            r2, l2 = self._walk_children(child, cpath)
            records.extend(r2)
            links.extend(l2)
        return records, links

    def _picture_fallback(
        self,
        should_parse: Callable[[Path], bool] | None = None,
    ) -> Iterator[tuple[dict[str, Any], list[tuple[str, str, str]]]]:
        """Картинки без ``CommonPictures/Х.xml`` — минимальный объект из ConfigDumpInfo."""
        info = self.dumps_dir / "ConfigDumpInfo.xml"
        if not info.is_file():
            return
        if should_parse is not None and not should_parse(info):
            return
        for name in _common_picture_names(self.dumps_dir):
            name = nfc(name)
            path = f"ОбщиеКартинки.{name}"
            if path in self._picture_paths:
                continue
            if (self.dumps_dir / "CommonPictures" / f"{name}.xml").is_file():
                continue
            props = {"Name": name, "ObjectBelonging": "Adopted"}
            rec = _dump_record(path, "ОбщиеКартинки", "CommonPicture", props)
            rec["source_rel"] = "ConfigDumpInfo.xml"
            yield rec, []
