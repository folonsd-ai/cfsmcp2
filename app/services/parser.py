from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from app.services.kinds import belong_normalize, kind_from_path

log = logging.getLogger("cfsmcp2.parser")

NODE_RE = re.compile(r"^(?P<tabs>\t*)-\s(?P<path>.+)$")
PROP_RE = re.compile(r"^(?P<tabs>\t*)(?P<key>[^:\t]+):\s*(?P<value>.*)$")
QUOTED_RE = re.compile(r'^"(.*)"$')
LIST_ITEM_RE = re.compile(r'^\t*"(.*)"\s*$')

# First chunk used for encoding decision (latin header + cp1251 body case).
SNIFF_BYTES = 8192
# Typical "cp1251 bytes decoded as latin-1/utf-8" markers for Russian text.
_MOJIBAKE_CHARS = frozenset(
    "ÐÑÒÓÔÕÖØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòôõöøùúûüýþ"
)
_REPLACEMENT = "\ufffd"


@dataclass
class ParsedNode:
    path: str
    props: dict[str, str | list[str]] = field(default_factory=dict)


@dataclass
class ReportMeta:
    config_name: str
    config_synonym: str
    version: str
    entity_type: str = "configuration"  # configuration | extension


@dataclass
class ParsedReport:
    config_name: str
    config_synonym: str
    version: str
    nodes: list[ParsedNode]


@dataclass
class TextDecodeStats:
    encoding: str = "utf-8"
    replacement_count: int = 0
    sniff_bytes: int = 0


@dataclass
class DecodeResult:
    text: str
    encoding: str
    replacement_count: int


def sniff_encoding(raw: bytes) -> str:
    """Detect report/BSL encoding from a sample (prefer ~8 KiB).

    UTF-8 is accepted only if the sample decodes cleanly *and* does not look like
    cp1251 mojibake (Ð/Ñ-heavy). Otherwise fall back to cp1251.
    """
    if not raw:
        return "utf-8"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "cp1251"
    non_ascii = [c for c in s if ord(c) > 127]
    if non_ascii:
        bad = sum(1 for c in non_ascii if c in _MOJIBAKE_CHARS)
        if bad / len(non_ascii) > 0.3:
            return "cp1251"
    return "utf-8"


def decode_bytes(raw: bytes, *, errors: str = "replace") -> DecodeResult:
    """Decode bytes with shared sniff + replacement counting."""
    encoding = sniff_encoding(raw[:SNIFF_BYTES] if len(raw) > SNIFF_BYTES else raw)
    text = raw.decode(encoding, errors=errors)
    return DecodeResult(
        text=text,
        encoding=encoding,
        replacement_count=text.count(_REPLACEMENT),
    )


def iter_report_lines(
    path: Path,
    stats: TextDecodeStats | None = None,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    progress_every: int = 2000,
) -> Iterator[str]:
    with path.open("rb") as bf:
        head = bf.read(SNIFF_BYTES)
    encoding = sniff_encoding(head)
    if stats is not None:
        stats.encoding = encoding
        stats.sniff_bytes = len(head)
    log.info("report encoding path=%s encoding=%s sniff=%sB", path.name, encoding, len(head))
    try:
        file_size = max(1, int(path.stat().st_size))
    except OSError:
        file_size = 1
    with path.open("r", encoding=encoding, errors="replace", newline="") as fh:
        n = 0
        for line in fh:
            clean = line.lstrip("\ufeff").rstrip("\r\n")
            if stats is not None and _REPLACEMENT in clean:
                stats.replacement_count += clean.count(_REPLACEMENT)
            n += 1
            if on_progress and n % progress_every == 0:
                try:
                    pos = int(fh.buffer.tell())
                except Exception:
                    pos = 0
                on_progress(min(pos, file_size), file_size)
            yield clean
        if on_progress:
            on_progress(file_size, file_size)


def iter_report_nodes(
    path: Path,
    stats: TextDecodeStats | None = None,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> Iterator[ParsedNode]:
    """Stream nodes one-by-one without accumulating the full report in RAM."""
    current: ParsedNode | None = None
    list_key: str | None = None

    for line in iter_report_lines(path, stats, on_progress=on_progress):
        if not line.strip():
            continue

        node_m = NODE_RE.match(line)
        if node_m:
            list_key = None
            if current is not None:
                yield current
            current = ParsedNode(path=node_m.group("path").strip())
            continue

        if current is None:
            continue

        prop_m = PROP_RE.match(line)
        if prop_m and not LIST_ITEM_RE.match(line):
            key = prop_m.group("key").strip()
            value_raw = prop_m.group("value").strip()
            if value_raw == "":
                list_key = key
                current.props[key] = []
                continue
            list_key = None
            qm = QUOTED_RE.match(value_raw)
            value = qm.group(1) if qm else value_raw
            current.props[key] = value
            continue

        item_m = LIST_ITEM_RE.match(line)
        if item_m and list_key is not None:
            items = current.props.get(list_key)
            if isinstance(items, list):
                items.append(item_m.group(1))
            continue

    if current is not None:
        yield current

    if stats is not None and stats.replacement_count:
        log.warning(
            "report decode replacements path=%s encoding=%s count=%s",
            path.name,
            stats.encoding,
            stats.replacement_count,
        )


def detect_entity_type(props: dict | None) -> str:
    """Classify report root as configuration (CF) or extension (CFE).

    From real ОтчетПоКонфигурации samples:
    - CFE: has non-empty ``НазначениеРасширенияКонфигурации`` (e.g. Дополнение)
      and usually root ``ПринадлежностьОбъекта=Заимствованный``.
    - CF: no purpose field; root ``ПринадлежностьОбъекта=Собственный``.
    ``РежимСовместимостиРасширенияКонфигурации`` alone is NOT decisive
    (CF also has it, often ``НеИспользовать``).
    """
    if not props:
        return "configuration"
    purpose = props.get("НазначениеРасширенияКонфигурации")
    if isinstance(purpose, str) and purpose.strip():
        return "extension"
    belong = props.get("ПринадлежностьОбъекта")
    if isinstance(belong, str):
        b = belong.strip().lower()
        if b == "заимствованный":
            return "extension"
        if b == "собственный":
            return "configuration"
    return "configuration"


def peek_report_meta(path: Path, fallback_stem: str) -> ReportMeta:
    """Read only until the first report node (config root) — enough for Имя/Синоним/Версия."""
    for node in iter_report_nodes(path):
        return meta_from_first_node(node, fallback_stem)
    return meta_from_first_node(None, fallback_stem)


_REPORT_NAME_RE = re.compile(
    r"(отч[её]т|report|конфигурац|configuration)",
    re.IGNORECASE,
)


def find_configuration_report_in_dir(directory: Path) -> Path | None:
    """Найти txt «Отчёт по конфигурации» в корне каталога (рядом с выгрузкой).

    Предпочитает имена с «Отчет»/«Report»/«Конфигурац»; если подходит один *.txt — его.
    """
    try:
        txts = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".txt"]
    except OSError:
        return None
    if not txts:
        return None
    scored: list[tuple[int, Path]] = []
    for p in txts:
        score = 0
        name = p.name
        if _REPORT_NAME_RE.search(name):
            score += 10
        if name.lower().startswith("отчет") or name.lower().startswith("отчёт"):
            score += 5
        scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], x[1].name.lower()))
    best_score, best = scored[0]
    if best_score > 0:
        return best
    if len(txts) == 1:
        return txts[0]
    return None


def meta_from_first_node(node: ParsedNode | None, fallback_stem: str) -> ReportMeta:
    config_name = fallback_stem
    config_synonym = ""
    version = ""
    entity_type = "configuration"
    if node is not None:
        if isinstance(node.props.get("Имя"), str) and node.props["Имя"]:
            config_name = str(node.props["Имя"])
        if isinstance(node.props.get("Синоним"), str):
            config_synonym = str(node.props["Синоним"])
        if isinstance(node.props.get("Версия"), str):
            version = str(node.props["Версия"])
        entity_type = detect_entity_type(node.props)
    return ReportMeta(
        config_name=config_name,
        config_synonym=config_synonym,
        version=version,
        entity_type=entity_type,
    )


def parse_report(path: Path) -> ParsedReport:
    """Compatibility helper: loads all nodes (prefer iter_report_nodes for large files)."""
    nodes = list(iter_report_nodes(path))
    meta = meta_from_first_node(nodes[0] if nodes else None, path.stem)
    return ParsedReport(
        config_name=meta.config_name,
        config_synonym=meta.config_synonym,
        version=meta.version,
        nodes=nodes,
    )


def node_fields(node: ParsedNode) -> dict:
    kind_ru, kind_en = kind_from_path(node.path)
    name = node.props.get("Имя")
    synonym = node.props.get("Синоним")
    comment = node.props.get("Комментарий")
    belong_raw = node.props.get("ПринадлежностьОбъекта")
    base = node.props.get("ОбъектРасширяемойКонфигурации")
    return {
        "path": node.path,
        "kind_ru": kind_ru,
        "kind": kind_en,
        "name": name if isinstance(name, str) else node.path.rsplit(".", 1)[-1],
        "synonym": synonym if isinstance(synonym, str) else "",
        "comment": comment if isinstance(comment, str) else "",
        "belong": belong_normalize(belong_raw if isinstance(belong_raw, str) else None),
        "base_object": base if isinstance(base, str) else "",
        "props": node.props,
    }


LINK_KEYS = {
    "Тип": "type",
    "Состав": "composition",
    "Движения": "movements",
    "ВводитсяНаОсновании": "based_on",
}


def extract_links(node: ParsedNode) -> list[tuple[str, str, str]]:
    """Return list of (from_path, to_ref, link_type)."""
    out: list[tuple[str, str, str]] = []
    for key, link_type in LINK_KEYS.items():
        val = node.props.get(key)
        if val is None:
            continue
        values: list[str]
        if isinstance(val, list):
            values = val
        elif isinstance(val, str) and val:
            values = [val]
        else:
            continue
        for v in values:
            v = v.strip()
            if v:
                out.append((node.path, v, link_type))
    return out
