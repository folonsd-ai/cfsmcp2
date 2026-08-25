"""Парсер BSL-модулей из dump-выгрузки (этап 4, ТЗ §15 п.4).

Источник — распакованные файлы ``dumps/e{id}/`` (или внешний каталог в
path-режиме): ``**/*.bsl`` и ``**/Ext/Form.bin`` (обычные формы, бинарный
контейнер). Для каждого модуля определяется объект-владелец (parent_path) и
роль (module_role) по относительному пути файла; из текста извлекаются методы
(``Процедура``/``Функция``): имя, сигнатура, флаг ``Экспорт``, тело (по
``bsl_load_mode``), номер строки, doc-комментарий, регион ``#Область``.

Строковые литералы (в т.ч. многострочные ``"…"`` + ``|``-продолжения) и
комментарии ``//`` маскируются, чтобы «КонецПроцедуры» внутри строки не
закрывал метод. Формат кодировок: UTF-8 (+ BOM), UTF-16 (+ BOM), cp1251.

``bsl_load_mode`` (см. ``normalize_bsl_load_mode``):
- ``signatures_only``: без тел в индексе, без рёбер ``calls`` (быстрый).
- ``signatures``: без тел в индексе; ``calls`` из тел в памяти при ingest.
  ``get_method`` / ``find_code_references`` читают ``.bsl`` с диска по ``source_rel``.
- ``full``: тела + регионы ``#Область``; ``calls`` как у signatures.
  Legacy ``code`` → ``full``.
- Модульный код вне процедур в v1 игнорируется.
"""

from __future__ import annotations

import logging
import re
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from app.services.kinds import CONTAINER_EN_TO_RU
from app.services.path_nfc import nfc_rel

log = logging.getLogger("cfsmcp2.bsl_parser")

# ---------------------------------------------------------------------------
# Роли модулей по имени файла модуля.
# ---------------------------------------------------------------------------
_MODULE_FILE_ROLES: dict[str, str] = {
    "Module.bsl": "Module",
    "ObjectModule.bsl": "ObjectModule",
    "ManagerModule.bsl": "ManagerModule",
    "RecordSetModule.bsl": "RecordSetModule",
    "RecordManagerModule.bsl": "RecordManagerModule",
    "ValueManagerModule.bsl": "ValueManagerModule",
    "CommandModule.bsl": "CommandModule",
}

# Топ-уровневые модули в ``Ext/`` (родитель — Конфигурация).
_TOP_LEVEL_ROLES: dict[str, str] = {
    "ManagedApplicationModule.bsl": "ManagedApplicationModule",
    "SessionModule.bsl": "SessionModule",
    "ExternalConnectionModule.bsl": "ExternalConnectionModule",
    "ClientApplicationModule.bsl": "ClientApplicationModule",
}

_HEAD_RE = re.compile(
    r"^\s*(?P<kw>Процедура|Функция|Procedure|Function)\s+"
    r"(?P<name>[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)",
    re.I,
)
_END_RE = re.compile(r"\b(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b", re.I)
_REGION_OPEN_RE = re.compile(r"^\s*#\s*(Область|Region)\s+(?P<name>\S+)", re.I)
_REGION_CLOSE_RE = re.compile(r"^\s*#\s*(КонецОбласти|EndRegion)\b", re.I)
_ANNOTATION_RE = re.compile(r"^\s*&", re.I)
_EXPORT_RE = re.compile(r"\bЭкспорт\b", re.I)
_CALL_RE = re.compile(r"(?P<name>[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\s*\(")

_MAX_LINE_BACKSCAN = 120


def decode_bsl(data: bytes) -> tuple[str, str]:
    """Декодирует BSL-байты в текст; возвращает (text, encoding)."""
    if not data:
        return "", "utf-8"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace"), "cp1251"


def iter_module_files(
    dumps_dir: Path,
    *,
    include_bsl: bool = True,
    include_form_bin: bool = True,
    files: list[Path] | None = None,
) -> Iterator[Path]:
    """Файлы-модули: ``**/*.bsl`` и/или ``**/Ext/Form.bin`` по флагам."""
    if files is not None:
        for p in files:
            if p.is_file():
                yield p
        return
    patterns: list[str] = []
    if include_bsl:
        patterns.append("*.bsl")
    if include_form_bin:
        patterns.append("*form.bin")
    if not patterns:
        return
    seen: set[Path] = set()
    for pattern in patterns:
        for p in sorted(dumps_dir.rglob(pattern)):
            if p.is_file() and p not in seen:
                seen.add(p)
                yield p


def classify_module_path(rel_path: str) -> tuple[str | None, str | None]:
    """Относительный путь файла → (parent_path, module_role) или (None, None).

    Канон путей — RU (ТЗ §6.2); контейнеры — из ``CONTAINER_EN_TO_RU``.
    Путь с диска/zip приводится к NFC (macOS NFD vs имена в XML).
    """
    parts = PurePosixPath(nfc_rel(rel_path)).parts
    if not parts:
        return None, None

    # Топ-уровневые модули: Ext/ManagedApplicationModule.bsl и т.п.
    if len(parts) == 2 and parts[0].lower() == "ext":
        role = _TOP_LEVEL_ROLES.get(parts[1])
        return ("Конфигурация", role) if role else (None, None)

    container = parts[0]
    ru_plural = CONTAINER_EN_TO_RU.get(container)
    if ru_plural is None or len(parts) < 3:
        return None, None
    name = parts[1]
    rest = parts[2:]
    base = f"{ru_plural}.{name}"

    # <C>/<Name>/Forms/<Form>/Ext/Form.bin — обычная форма (hierarchical dump)
    if (
        len(rest) >= 4
        and rest[0] == "Forms"
        and rest[2] == "Ext"
        and rest[3].lower() == "form.bin"
    ):
        return f"{base}.Формы.{rest[1]}", "Form"

    # <C>/<Name>/Forms/<Form>/Ext/Form/Module.bsl → <Объект>.Формы.<Форма>
    if (
        len(rest) >= 5
        and rest[0] == "Forms"
        and rest[2] == "Ext"
        and rest[3] == "Form"
        and rest[4].lower() == "module.bsl"
    ):
        return f"{base}.Формы.{rest[1]}", "Form"

    # CommonForms/<Name>/Ext/Form.bin — обычная общая форма
    if (
        len(rest) >= 2
        and rest[0] == "Ext"
        and rest[1].lower() == "form.bin"
    ):
        return base, "Form"

    # CommonForms/<Name>/Ext/Form/Module.bsl → ОбщиеФормы.<Имя> (роль Form)
    if (
        len(rest) >= 3
        and rest[0] == "Ext"
        and rest[1] == "Form"
        and rest[2].lower() == "module.bsl"
    ):
        return base, "Form"

    # <C>/<Name>/Commands/<Cmd>/Ext/CommandModule.bsl → <Объект>.Команды.<Команда>
    if (
        len(rest) >= 4
        and rest[0] == "Commands"
        and rest[2] == "Ext"
        and rest[3].lower() == "commandmodule.bsl"
    ):
        return f"{base}.Команды.{rest[1]}", "CommandModule"

    # <C>/<Name>/Ext/<ModuleFile>.bsl
    if len(rest) >= 2 and rest[0] == "Ext":
        role = _MODULE_FILE_ROLES.get(rest[1])
        if role:
            return base, role
    return None, None


def _scan_line(line: str, in_string: bool) -> tuple[str, bool]:
    """Маскирует содержимое строковых литералов и комментариев ``//``.

    Возвращает (masked_line, in_string_after). Внутри многострочной строки
    продолжение идёт по строке, начинающейся с ``|`` (после пробелов/табуляции).
    ``""`` внутри строки — экранированная кавычка, строку не закрывает.
    """
    out: list[str] = []
    i = 0
    n = len(line)
    if in_string:
        while i < n and line[i] in " \t":
            out.append(" ")
            i += 1
        if i < n and line[i] == "|":
            out.append(" ")
            i += 1
    while i < n:
        ch = line[i]
        if in_string:
            if ch == '"':
                if i + 1 < n and line[i + 1] == '"':
                    out.append(" ")
                    out.append(" ")
                    i += 2
                    continue
                in_string = False
                out.append(" ")
                i += 1
                continue
            out.append(" ")
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(" ")
            i += 1
            continue
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            out.append(" " * (n - i))
            break
        out.append(ch)
        i += 1
    return "".join(out), in_string


def _paren_balance(masked: str) -> int:
    return masked.count("(") - masked.count(")")


def _strip_comment(line: str) -> str:
    """Убирает ведущие пробелы и ``//`` из строки doc-комментария."""
    s = line.strip()
    if s.startswith("//"):
        s = s[2:].lstrip()
    return s


def method_path(parent: str, role: str, name: str) -> str:
    return f"{parent}.Методы.{role}.{name}"


def parse_module(
    text: str,
    parent: str,
    role: str,
    source_file: str,
    load_mode: str,
    exclude_subs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Разбирает текст модуля в список методов.

    Каждый элемент: поля объекта + служебный ``calls`` (если режим с графом)
    и ``source_line``.
    """
    from app.schemas.entities import (
        bsl_extracts_calls,
        bsl_stores_body,
        normalize_bsl_load_mode,
    )

    load_mode = normalize_bsl_load_mode(load_mode)
    store_body = bsl_stores_body(load_mode)
    want_calls = bsl_extracts_calls(load_mode)
    exclude = [str(s).strip().lower() for s in (exclude_subs or []) if str(s).strip()]
    lines = text.split("\n")
    n = len(lines)
    i = 0
    in_string = False
    region_stack: list[tuple[str, int]] = []
    recent_raw: list[str] = []
    recent_masked: list[str] = []
    methods: list[dict[str, Any]] = []

    def _doc_comment(header_idx: int) -> str:
        # ``recent_raw`` держит последние _MAX_LINE_BACKSCAN строк; смещение
        # относительно глобального индекса считается отрицательной индексацией.
        doc_lines: list[str] = []
        dist = 1
        while dist <= len(recent_raw) - 1 and dist < _MAX_LINE_BACKSCAN:
            mraw = recent_raw[-(dist + 1)]
            if mraw.strip() == "":
                dist += 1
                continue
            if _ANNOTATION_RE.match(mraw):
                dist += 1
                continue
            # ``//`` в начале строки (после пробелов) всегда комментарий — строка
            # внутри многострочного литерала начинается с ``|``.
            if mraw.lstrip().startswith("//"):
                doc_lines.append(_strip_comment(mraw))
                dist += 1
                continue
            break
        doc_lines.reverse()
        return "\n".join(doc_lines)

    while i < n:
        raw = lines[i]
        masked, in_string = _scan_line(raw, in_string)
        recent_raw.append(raw)
        recent_masked.append(masked)
        if len(recent_raw) > _MAX_LINE_BACKSCAN:
            recent_raw.pop(0)
            recent_masked.pop(0)

        stripped = masked.strip()
        m = _REGION_OPEN_RE.match(stripped)
        if m:
            region_stack.append((m.group("name").strip(), i + 1))
            i += 1
            continue
        if _REGION_CLOSE_RE.match(stripped):
            if region_stack:
                region_stack.pop()
            i += 1
            continue

        h = _HEAD_RE.match(stripped)
        if not h:
            i += 1
            continue

        kw = h.group("kw")
        kind = "Procedure" if kw.lower() in ("процедура", "procedure") else "Function"
        name = h.group("name")
        if exclude and any(s in name.lower() for s in exclude):
            i += 1
            continue

        # Заголовок: до баланса скобок (поддерживаются многострочные сигнатуры).
        header_raw: list[str] = [raw]
        header_masked = masked
        parens = _paren_balance(masked)
        j = i + 1
        while parens > 0 and j < n:
            hraw = lines[j]
            hmasked, in_string = _scan_line(hraw, in_string)
            header_raw.append(hraw)
            header_masked += "\n" + hmasked
            parens += _paren_balance(hmasked)
            j += 1
        header_idx = j - 1  # последняя строка заголовка (0-based)

        export = bool(_EXPORT_RE.search(header_masked))
        signature = "\n".join(header_raw)
        doc = _doc_comment(i)
        region = region_stack[-1][0] if region_stack else ""
        region_line = region_stack[-1][1] if region_stack else 0

        # Однострочный метод: КонецПроцедуры в самом заголовке.
        if _END_RE.search(header_masked):
            body_raw: list[str] = []
            end_line = header_idx
        else:
            body_raw = []
            end_line = header_idx
            k = header_idx + 1
            found_end = False
            while k < n:
                braw = lines[k]
                bmasked, in_string = _scan_line(braw, in_string)
                if _END_RE.search(bmasked):
                    end_line = k
                    found_end = True
                    break
                body_raw.append(braw)
                k += 1
            if not found_end:
                # Незакрытый метод (повреждённый модуль) — берём до конца файла.
                end_line = n - 1

        body = "\n".join(body_raw)
        props: dict[str, Any] = {
            "parent_path": parent,
            "module_role": role,
            "export": bool(export),
            "signature": signature,
            "load_mode": load_mode,
        }
        if store_body:
            props["body"] = body
        if load_mode == "full":
            props["region"] = region
            props["region_line"] = int(region_line)
        props["source_file"] = source_file
        props["line"] = int(i + 1)

        calls: list[str] = []
        # CALLS: signatures (с графом) и full — тело в памяти; в индекс body
        # кладём только при full. signatures_only — без extract/insert calls.
        if want_calls and body:
            calls = sorted(extract_call_names(body))

        methods.append(
            {
                "path": method_path(parent, role, name),
                "kind_ru": "Процедура" if kind == "Procedure" else "Функция",
                "kind": kind,
                "name": name,
                "synonym": "",
                "comment": doc,
                "belong": "Own",
                "base_object": "",
                "props": props,
                "calls": calls,
                "source_line": int(i + 1),
            }
        )
        i = end_line + 1

    return methods


def resolve_dump_file(dumps_dir: Path | str, source_rel: str) -> Path | None:
    """Файл модуля внутри каталога выгрузки; None если rel пустой, снаружи корня или нет файла."""
    rel = nfc_rel(source_rel)
    if not rel or rel.startswith("../") or "/../" in f"/{rel}/":
        return None
    root = Path(dumps_dir)
    if not root.is_dir():
        return None
    try:
        root_res = root.resolve()
    except OSError:
        return None
    candidate = root.joinpath(*PurePosixPath(rel).parts)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root_res)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def read_module_text(dumps_dir: Path | str, source_rel: str) -> str:
    """Текст BSL с диска (``.bsl`` или извлечённый ``Form.bin``). Пустая строка, если нет файла."""
    path = resolve_dump_file(dumps_dir, source_rel)
    if path is None:
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if path.name.lower() == "form.bin":
        return extract_bsl_from_form_bin(data) or ""
    text, _enc = decode_bsl(data)
    return text


def extract_method_body(text: str, *, name: str, line: int | None = None) -> str:
    """Тело процедуры/функции из текста модуля (без заголовка и Конец*)."""
    want = (name or "").casefold()
    if not text or not want:
        return ""
    parsed = parse_module(text, "_", "Module", "", "code")
    named = [m for m in parsed if (m.get("name") or "").casefold() == want]
    if named:
        pool = named
    elif line:
        pool = parsed
    else:
        return ""
    if not pool:
        return ""
    if line:
        def _dist(m: dict[str, Any]) -> int:
            ln = int((m.get("props") or {}).get("line") or 0)
            return abs(ln - int(line))

        best = min(pool, key=_dist)
        if not named and abs(int((best.get("props") or {}).get("line") or 0) - int(line)) > 2:
            return ""
    else:
        best = pool[0]
    return str((best.get("props") or {}).get("body") or "")


def extract_call_names(body: str) -> set[str]:
    """Имена вызываемых методов по шаблону ``ИмяМетода(`` (вне строк/комментариев)."""
    names: set[str] = set()
    in_string = False
    for line in body.split("\n"):
        masked, in_string = _scan_line(line, in_string)
        for m in _CALL_RE.finditer(masked):
            names.add(m.group("name"))
    return names


# ---------------------------------------------------------------------------
# Form.bin обычных форм: UTF-8 blob (cfsmcp) + zlib fallback.
# ---------------------------------------------------------------------------
_MAX_DECOMPRESSED = 16 * 1024 * 1024


def _decompress_limited(raw: bytes) -> bytes | None:
    try:
        d = zlib.decompressobj()
        out = d.decompress(raw, _MAX_DECOMPRESSED)
        if d.unconsumed_tail:
            return None
        return out
    except zlib.error:
        return None


def extract_bsl_from_form_bin(data: bytes) -> str | None:
    """Извлекает текст модуля из ``Form.bin`` (обычные формы).

    Hierarchical dump: несжатый UTF-8 внутри контейнера (как в cfsmcp).
    Если маркеров нет — zlib-блоки / сигнатуры. None = пустая форма или сбой.
    """
    from app.services.form_bin import FormBinEmpty, FormBinError, extract_form_module_bsl

    try:
        return extract_form_module_bsl(data)
    except (FormBinEmpty, FormBinError):
        pass
    return _extract_bsl_from_form_bin_zlib(data)


def _extract_bsl_from_form_bin_zlib(data: bytes) -> str | None:
    """Редкий контейнер: zlib-фрагменты вместо UTF-8 blob."""
    parts: list[bytes] = []
    if len(data) >= 6:
        pos = 2
        while pos + 4 <= len(data):
            length = int.from_bytes(data[pos : pos + 4], "little")
            pos += 4
            if length <= 0 or length > _MAX_DECOMPRESSED or pos + length > len(data):
                break
            chunk = data[pos : pos + length]
            pos += length
            dec = _decompress_limited(chunk)
            if dec:
                parts.append(dec)

    if not parts:
        for m in re.finditer(rb"x\x9c|x\xda|x\x01", data):
            dec = _decompress_limited(data[m.start() :])
            if dec:
                parts.append(dec)

    for part in parts:
        if not part:
            continue
        text, _enc = decode_bsl(part)
        if _looks_like_bsl(text):
            return text
    return None


def _looks_like_bsl(text: str) -> bool:
    head = text[:2000]
    return (
        "КонецПроцедуры" in head
        or "КонецФункции" in head
        or ("Процедура" in head and "Функция" in head)
    )
