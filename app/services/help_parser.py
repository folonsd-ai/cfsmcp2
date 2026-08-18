"""Парсер пользовательской справки конфигурации (U10).

Hierarchical-выгрузка хранит справку как ``**/Ext/Help/*.html`` (обычно
``ru.html``) рядом с каждым объектом, у которого она есть. 1С-формат —
простой UTF-8 HTML: ``h1`` (имя объекта), ``h2``/``h3`` (разделы), ``p``/``ul``.

Задачи модуля:
- собрать все ``*.html`` под ``**/Help/**`` и построить owner-path по пути файла
  (``Catalogs/Х/Ext/Help/ru.html`` → ``Справочники.Х``, ``…/Forms/F/Ext/Help`` →
  ``….Формы.F``);
- разбить страницу на разделы по ``h2``/``h3`` — каждый раздел становится
  отдельным record kind=``Help`` (лучше recall, чем один документ на файл,
  как в связке code-meta);
- record содержит owner в ``props`` и связь owner → справку добавляет
  конвейер (link_type='help').

Реализован на stdlib ``html.parser`` — без новых зависимостей.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from app.services.kinds import CONTAINER_EN_TO_RU
from app.services.path_nfc import nfc_rel

log = logging.getLogger("cfsmcp2.help_parser")

# kind_ru / kind для справки.
HELP_KIND_RU = "Справка"
HELP_KIND = "Help"
# Префикс путей help-объектов («Help.Документы.X.1»).
HELP_PATH_PREFIX = "Help."
# Максимум символов на один чанк-раздел (страницы бывают большими, > 500 КБ).
MAX_SECTION_CHARS = 2600
# Если в файле нет ни одного h2/h3 — весь текст одним чанком.
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})

# EN-единственное имя объекта (папка верхнего уровня контейнера) → RU-путь.
_OWNER_CONTAINERS = dict(CONTAINER_EN_TO_RU)


def _profile_flag(profile: dict, key: str, default: bool = False) -> bool:
    v = profile.get(key)
    return v if isinstance(v, bool) else default


def _is_help_html_rel(rel: PurePosixPath) -> bool:
    parts = [p.lower() for p in rel.parts]
    if "help" not in parts:
        return False
    if rel.suffix.lower() not in (".html", ".htm"):
        return False
    # Типичная раскладка: …/Ext/Help/ru.html. Пропускаем только сам Help.
    if parts[-1] in ("help", "contents.html", "map.html"):
        return False
    return True


is_help_html_rel = _is_help_html_rel


def _resolve_owner_path(rel: str) -> str | None:
    """Путь файла → owner-path объекта 1С.

    ``Catalogs/Х/Ext/Help/ru.html`` → ``Справочники.Х``
    ``Documents/Х/Forms/F/Ext/Help/ru.html`` → ``Документы.Х.Формы.F``
    ``CommonForms/Х/Ext/Help/ru.html`` → ``ОбщиеФормы.Х``
    ``Subsystems/Х/Subsystems/Y/Ext/Help/ru.html`` → ``Подсистемы.Х.Подсистемы.Y``
    Возвращает None для нераспознаваемых путей.
    """
    p = PurePosixPath(nfc_rel(rel))
    parts = [x for x in p.parts if x not in ("", ".")]
    if len(parts) < 4 or parts[-2].lower() != "help" or parts[-3].lower() != "ext":
        return None
    top = parts[0]
    if top == "CommonForms" and len(parts) >= 4:
        return f"ОбщиеФормы.{parts[1]}"
    if top == "CommonTemplates" and len(parts) >= 4:
        return f"ОбщиеМакеты.{parts[1]}"
    ru_container = _OWNER_CONTAINERS.get(top)
    if not ru_container:
        return None
    obj = parts[1]
    body = parts[2:-3]  # например ['Forms','F'] или []
    tail: list[str] = []
    i = 0
    while i < len(body):
        seg = body[i]
        if seg in ("Forms", "Templates") and i + 1 < len(body):
            tail.append("Формы" if seg == "Forms" else "Макеты")
            tail.append(body[i + 1])
            i += 2
        else:
            tail.append(seg)
            i += 1
    owner = f"{ru_container}.{obj}"
    if tail:
        owner += "." + ".".join(tail)
    return owner


def _humanize_camel(name: str) -> str:
    """CamelCase → человекочитаемое (ТранспортнаяНакладная → «транспортная накладная»)."""
    import re

    words = re.findall(r"[A-ZА-ЯЁ][^A-ZА-ЯЁ]*", name or "")
    return " ".join(w.lower() for w in words if w)


def _title_from_path(rel: str, fallback: str) -> str:
    """Синтетический заголовок из пути (если в HTML нет h1)."""
    owner = _resolve_owner_path(rel)
    if owner:
        return owner.replace(".", " / ")
    return fallback or Path(rel).stem


class _HelpHtmlParser(HTMLParser):
    """Собирает (title, [sections]) из ru.html.

    Section = (заголовок | None, текст). Текст накапливается между h2/h3;
    h4 считается частью текущего раздела (текст с подзаголовком).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str = ""
        self.sections: list[tuple[str, str]] = []
        self._cur_heading: str = ""
        self._cur_text: list[str] = []
        self._skip_depth = 0
        self._capture_text = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        if tag in _HEADING_TAGS:
            self._flush_section()
            self._capture_text = True
            self._cur_heading = ""
            return
        if tag == "script" or tag == "style":
            self._skip_depth += 1
            return
        if tag in ("p", "li", "br", "tr", "h4", "h5", "h6", "div"):
            self._cur_text.append(" ")
            return
        self._capture_text = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag in ("p", "li", "tr", "h4", "h5", "h6", "div"):
            self._cur_text.append(" ")
            return

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if not self._capture_text:
            return
        text = data.strip()
        if not text:
            return
        if self._cur_heading is not None and not self._cur_text and self._cur_heading == "":
            # Первый фрагмент после <hN> — это и есть заголовок раздела
            self._cur_heading = text
        else:
            self._cur_text.append(text)
            self._capture_text = True

    def _flush_section(self) -> None:
        if not self._cur_heading and not self._cur_text:
            return
        body = " ".join(self._cur_text).strip()
        if body or self._cur_heading:
            self.sections.append((self._cur_heading or "", body))
        self._cur_heading = ""
        self._cur_text = []
        self._capture_text = False

    def finish(self) -> tuple[str, list[tuple[str, str]]]:
        self._flush_section()
        return self.title, self.sections


def parse_help_file(html_path: Path) -> tuple[str, list[tuple[str, str]]]:
    """Парсит один HTML-файл справки → (title, [(heading, body), …]).

    h1 становится title документа (если есть), остальные секции — разделы.
    """
    data = html_path.read_bytes()
    for enc in ("utf-8", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="replace")
    parser = _HelpHtmlParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        log.debug("help html parse failed %s", html_path, exc_info=True)
        return "", []
    title, sections = parser.finish()
    # h1 (если парсер его не взял как секцию) → title. Упрощённо:
    # первый заголовок без текста до него уже попал в sections[0] (heading).
    if title:
        return title, sections
    # Пробуем восстановить title из h1: если первая секция имеет heading
    # и текст — используем heading как title при отсутствии контента.
    clean = [(h.strip(), b.strip()) for h, b in sections if h or b]
    return title, clean


def _split_long_section(text: str, max_chars: int) -> list[str]:
    """Бьёт длинный раздел по абзацам, не превышая max_chars на фрагмент."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts: list[str] = []
    cur = ""
    for para in text.split(". "):
        piece = para if cur == "" else ". " + para
        if len(cur) + len(piece) > max_chars and cur:
            parts.append(cur)
            cur = para
        else:
            cur += piece
    if cur:
        parts.append(cur)
    return parts


def iter_help_file_records(
    dumps_dir: Path, rel: str
) -> Iterator[dict[str, Any]]:
    """Генерирует record'ы kind=Help для одного файла справки.

    record: {path, kind_ru, kind, name, synonym, comment, belong, base_object, props}
    path: ``Help.<owner>`` для вводного чанка, ``Help.<owner>.<N>`` для разделов.
    props: {owner, section, sections_total, source_rel}.
    """
    owner = _resolve_owner_path(rel)
    if not owner:
        log.debug("help: owner not resolved for %s", rel)
        return
    html_path = dumps_dir / rel.replace("/", "\\") if "\\" in str(dumps_dir) else dumps_dir / rel
    if not html_path.is_file():
        html_path = dumps_dir.joinpath(*PurePosixPath(rel).parts)
    if not html_path.is_file():
        return
    title, sections = parse_help_file(html_path)
    base_title = title.strip() or _title_from_path(rel, owner.rsplit(".", 1)[-1])

    chunks: list[tuple[str, str, str]] = []  # (heading, body, name)
    if not sections:
        chunks.append(("", "", base_title))
    for i, (heading, body) in enumerate(sections):
        if i == 0 and not heading and body:
            # Интро без заголовка — вводный чанк
            chunks.append(("", body, base_title))
            continue
        if not body:
            continue
        for piece in _split_long_section(body, MAX_SECTION_CHARS):
            chunks.append((heading, piece, heading or base_title))

    if not chunks:
        return
    total = len(chunks)
    for idx, (heading, body, name) in enumerate(chunks):
        chunk_path = f"{HELP_PATH_PREFIX}{owner}" if idx == 0 else f"{HELP_PATH_PREFIX}{owner}.{idx}"
        props = {
            "owner": owner,
            "section": heading,
            "sections_total": total,
            "source_rel": rel.replace("\\", "/"),
        }
        yield {
            "path": chunk_path,
            "kind_ru": HELP_KIND_RU,
            "kind": HELP_KIND,
            "name": name,
            "synonym": heading or name,
            "comment": body,
            "belong": "Own",
            "base_object": "",
            "source_rel": rel.replace("\\", "/"),
            "props": props,
        }


def iter_dump_help_records(
    dumps_dir: Path,
    profile: dict,
    should_parse: Callable[[Path], bool] | None = None,
    *,
    files: list[Path] | None = None,
) -> Iterator[dict[str, Any]]:
    """Обход всех ``**/Help/*.html`` в Hierarchical-выгрузке (U10)."""
    if not _profile_flag(profile, "help", True):
        return
    seen: set[str] = set()
    html_iter = files if files is not None else sorted(dumps_dir.rglob("*.html"))
    for p in html_iter:
        if not p.is_file():
            continue
        rel = nfc_rel(p.relative_to(dumps_dir).as_posix())
        if files is None and not _is_help_html_rel(PurePosixPath(rel)):
            continue
        if rel in seen:
            continue
        seen.add(rel)
        if should_parse is not None and not should_parse(p):
            continue
        yield from iter_help_file_records(dumps_dir, rel)
