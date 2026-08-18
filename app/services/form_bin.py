"""Извлечение модуля обычной формы из ``Form.bin`` (hierarchical dump).

Типичный путь: ``…/Forms/<Form>/Ext/Form.bin``. В выгрузке 1С модуль лежит
несжатым UTF-8 внутри контейнера вместе с сериализацией формы (не zlib).
Порт из cfsmcp ``app/services/form_bin.py`` / ``Модуль-формы-из-Form.bin.md``.
"""

from __future__ import annotations

import re


class FormBinError(ValueError):
    """Form.bin does not contain a usable UTF-8 BSL module."""


class FormBinEmpty(FormBinError):
    """Form.bin is present but has no BSL module (UI-only / empty module).

    Not a corrupt container — skip quietly; do not count as extract failure.
    """


_PROC = "Процедура".encode("utf-8")
_FUNC = "Функция".encode("utf-8")
_END_PROC = "КонецПроцедуры".encode("utf-8")
_END_FUNC = "КонецФункции".encode("utf-8")
_PROC_1251 = "Процедура".encode("cp1251")
_FUNC_1251 = "Функция".encode("cp1251")
_TEXT_OK = frozenset({9, 10, 13}) | frozenset(range(32, 127))


def form_bin_looks_empty(data: bytes) -> bool:
    """True when the container has no procedure/function markers (UTF-8 or cp1251)."""
    if not data:
        return True
    if data.find(_PROC) >= 0 or data.find(_FUNC) >= 0:
        return False
    if data.find(_PROC_1251) >= 0 or data.find(_FUNC_1251) >= 0:
        return False
    return True


def is_form_bin_path(path: str) -> bool:
    p = path.replace("\\", "/").rstrip("/")
    return p.lower().endswith("/ext/form.bin") or p.lower() == "form.bin"


def form_bin_to_module_path(path: str) -> str:
    """Map ``…/Ext/Form.bin`` → ``…/Ext/Form/Module.bsl`` (managed twin)."""
    p = path.replace("\\", "/").lstrip("/")
    while "//" in p:
        p = p.replace("//", "/")
    if not is_form_bin_path(p):
        raise ValueError(f"not a Form.bin path: {path}")
    base = p.rsplit("/", 1)[0]  # …/Ext
    return f"{base}/Form/Module.bsl"


def diagnose_form_bin(data: bytes) -> str:
    """Human-readable diagnostics when Form.bin extract fails."""
    size = len(data)
    if size == 0:
        return "size=0 | empty_file"
    nul = data.count(0)
    ascii_like = sum(1 for b in data if b in _TEXT_OK or b >= 0x80)
    parts = [
        f"size={size}",
        f"nul_bytes={nul}",
        f"textish_ratio={ascii_like / size:.2f}",
        f"has_utf8_Процедура={data.find(_PROC) >= 0}",
        f"has_utf8_Функция={data.find(_FUNC) >= 0}",
        f"has_utf8_КонецПроцедуры={data.find(_END_PROC) >= 0}",
        f"has_cp1251_Процедура={data.find(_PROC_1251) >= 0}",
        f"has_cp1251_Функция={data.find(_FUNC_1251) >= 0}",
        f"head_hex={data[:24].hex()}",
    ]
    head = bytes(b if 32 <= b < 127 else 0x2E for b in data[:48])
    parts.append(f"head_ascii={head.decode('ascii', errors='replace')!r}")
    if data.find(_PROC) < 0 and data.find(_PROC_1251) >= 0:
        parts.append("hint=module_may_be_cp1251_not_utf8")
    elif data.find(_PROC) < 0 and data.find(_FUNC) < 0:
        parts.append("hint=no_bsl_keywords_likely_empty_or_binary_only_form")
    return " | ".join(parts)


def _textish_bytes(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == 0:
            out.append(10)  # NUL → newline (module often NUL-separated)
        elif b in _TEXT_OK or b >= 0x80:
            out.append(b)
        else:
            out.append(10)
    return bytes(out)


def _trim_module_text(raw: str) -> str | None:
    raw = raw.lstrip("\r\n")
    raw = re.sub(
        r"^[0-9a-fA-F]+\s+[0-9a-fA-F]+\s+[0-9a-fA-F]+\s*\r?\n",
        "",
        raw,
        count=1,
    )
    raw = raw.lstrip("\ufeff").lstrip("\r\n")

    ends = [m.end() for m in re.finditer(r"КонецПроцедуры|КонецФункции", raw)]
    if not ends:
        return None

    tail_from = ends[-1]
    cut_candidates: list[int] = []
    for pat in (
        r"\r?\n[0-9a-fA-F]{8}\s+[0-9a-fA-F]{8}\s+7fffffff",
        r"\r?\n\ufeff?\{27,",
        r"\ufeff\{27,",
    ):
        m = re.search(pat, raw[tail_from:])
        if m:
            cut_candidates.append(tail_from + m.start())
    m = re.search(r"\n\{[0-9]", raw[tail_from:]) or re.search(r"\n\{", raw[tail_from:])
    if m:
        cut_candidates.append(tail_from + m.start())

    end = min(cut_candidates) if cut_candidates else len(raw)
    module = raw[:end].rstrip() + "\n"

    cut = re.search(r"\n[0-9a-fA-F]{8}\s+[0-9a-fA-F]{8}\s+7fffffff", module)
    if cut:
        module = module[: cut.start()].rstrip() + "\n"

    if "{27," in module or re.search(
        r"\n[0-9a-fA-F]{8}\s+[0-9a-fA-F]{8}\s+7fffffff", module
    ):
        return None
    if ("Процедура" not in module) and ("Функция" not in module):
        return None
    return module


def _extract_by_keyword_span(data: bytes) -> str | None:
    """Fallback: slice from first Процедура/Функция to last Конец* in byte stream."""
    starts = [i for i in (data.find(_PROC), data.find(_FUNC)) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end_pos = -1
    for marker in (_END_PROC, _END_FUNC):
        pos = data.rfind(marker)
        if pos > start and pos > end_pos:
            end_pos = pos + len(marker)
    if end_pos < 0:
        return None
    chunk = _textish_bytes(data[start:end_pos])
    try:
        text = chunk.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = chunk.decode("utf-8", errors="replace")
    return _trim_module_text(text)


def extract_form_module_bsl(data: bytes) -> str:
    """Return clean UTF-8 BSL module text extracted from Form.bin bytes."""
    if form_bin_looks_empty(data):
        raise FormBinEmpty(
            f"no BSL module in Form.bin (empty/UI-only) | {diagnose_form_bin(data)}"
        )
    if data.find(_PROC) < 0 and data.find(_FUNC) < 0:
        raise FormBinError(
            f"no UTF-8 BSL (Процедура) in Form.bin | {diagnose_form_bin(data)}"
        )

    frags: list[str] = []
    buf = bytearray()

    def flush() -> None:
        nonlocal buf
        if len(buf) < 12:
            buf = bytearray()
            return
        try:
            s = buf.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            s = buf.decode("utf-8", errors="replace")
            if (
                ("Процедура" not in s)
                and ("Функция" not in s)
                and ("КонецПроцедуры" not in s)
            ):
                buf = bytearray()
                return
        if ("Процедура" in s) or ("Функция" in s) or ("КонецПроцедуры" in s):
            frags.append(s)
        buf = bytearray()

    for b in data:
        if b == 0:
            flush()
        elif b in _TEXT_OK or b >= 0x80:
            buf.append(b)
        else:
            flush()
    flush()

    raw: str | None = None
    if frags:
        raw = max(frags, key=len)
        module = _trim_module_text(raw)
        if module:
            return module

    loose = _extract_by_keyword_span(data)
    if loose:
        return loose

    if not frags:
        raise FormBinError(f"no BSL text fragments found | {diagnose_form_bin(data)}")
    preview = (raw or "")[:120].replace("\n", "\\n")
    raise FormBinError(
        f"no КонецПроцедуры/КонецФункции or trim failed | frag_len={len(raw or '')} | "
        f"frag_preview={preview!r} | {diagnose_form_bin(data)}"
    )
