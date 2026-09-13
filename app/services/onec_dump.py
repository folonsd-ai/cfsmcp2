"""1C Configurator batch dump engine (no UI / FastAPI dependencies)."""
from __future__ import annotations

import locale
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from app.services.dump_parser import read_configuration_meta
from app.services.onec_platform import validate_platform

log = logging.getLogger("cfsmcp2.onec_dump")

CREATE_NO_WINDOW = 0x08000000

# cfsmcp2 ingest expects hierarchical dump (ARCHITECTURE §3.1); never rely on platform default.
DUMP_FORMAT = "Hierarchical"
_MIN_HIERARCHICAL_PLATFORM = (8, 3, 7, 0)

_DUMP_MARKERS = (
    "Configuration.xml",
    "ConfigDumpInfo.xml",
)

_ERROR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"нет\s+свободн\w*\s+лиценз", re.I), "нет свободной лицензии"),
    (re.compile(r"no\s+free\s+licen", re.I), "нет свободной лицензии"),
    (re.compile(r"баз\w*\s+данн\w*\s+заблок", re.I), "база заблокирована или занята"),
    (re.compile(r"database\s+is\s+locked", re.I), "база заблокирована или занята"),
    (
        re.compile(
            r"неверн\w*\s+(?:имя\s+)?пользовател\w*|incorrect\s+password|wrong\s+password|"
            r"пользователь\s+иб\s+не\s+идентифицирован",
            re.I,
        ),
        "неверное имя пользователя или пароль",
    ),
    (
        re.compile(
            r"конфигура(?:тор|ция)\w*.*(?:открыт|занят)|configuration\s+is\s+opened",
            re.I,
        ),
        "конфигурация открыта в конфигураторе",
    ),
    (
        re.compile(r"(?:недоступ\w*|отказ\w*|access\s+denied).*(?:запис|write|directory|каталог)", re.I),
        "каталог недоступен для записи",
    ),
    (re.compile(r"ошибка\s+доступа\s+к\s+файлу", re.I), "каталог недоступен для записи"),
)


@dataclass(slots=True)
class DumpProfileSpec:
    """Runtime dump profile (stage 2 — not tied to DB yet)."""

    name: str = ""
    target_type: str = "configuration"  # configuration | extension
    extension_name: str = ""
    ib_type: str = "file"  # file | server
    ib_address: str = ""
    ib_user: str = ""
    password: str = field(default="", repr=False)
    platform_path: str = ""
    out_dir: str = ""
    dump_mode: str = "update"  # update | full
    clear_before_full: bool = False
    timeout_sec: int = 7200


@dataclass(slots=True)
class PreflightResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DumpRunResult:
    success: bool
    return_code: int | None = None
    reason: str = ""
    log_tail: str = ""
    log_path: str = ""
    file_count: int = 0
    cancelled: bool = False
    bootstrap_used: bool = False


@dataclass(slots=True)
class _PreparedLaunch:
    argv: list[str]
    params_path: Path
    out_log_path: Path


def classify_log_error(log_text: str) -> str:
    """Map typical Configurator /Out messages to short human reasons."""
    if not log_text:
        return ""
    for pattern, label in _ERROR_PATTERNS:
        if pattern.search(log_text):
            return label
    return ""


def _parse_version_tuple(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in (version or "").split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _quote_cli_value(value: str) -> str:
    """Quote value for 1C response file when spaces or quotes are present (metod8dev-2272)."""
    if re.search(r'[\s"]', value):
        return '"' + value.replace('"', '""') + '"'
    return value


def _param_line(key: str, value: str | None = None) -> str:
    """One response-file line: ``/Key value`` or flag-only ``/Key``."""
    if value is None:
        return key
    return f"{key} {_quote_cli_value(value)}"


def _build_dump_config_line(profile: DumpProfileSpec) -> str:
    parts = [
        "/DumpConfigToFiles",
        _quote_cli_value(profile.out_dir),
        "-format",
        DUMP_FORMAT,
    ]
    if profile.target_type == "extension":
        name = profile.extension_name.strip()
        if not name:
            raise ValueError("extension_name is required for extension target")
        parts.extend(["-Extension", _quote_cli_value(name)])
    if profile.dump_mode == "update":
        parts.append("-update")
    return " ".join(parts)


def count_files_in_directory(directory: str | Path) -> int:
    """Count regular files under *directory* (progress indicator for UI stage 6)."""
    root = Path(directory)
    if not root.is_dir():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        total += len(filenames)
    return total


def _read_out_log(path: Path) -> str:
    """Read Configurator /Out log; platform writes UTF-8 with BOM (S1, bytes ef bb bf)."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", system_ansi_encoding()):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _tail_text(text: str, *, max_lines: int = 40, max_chars: int = 8000) -> str:
    lines = text.splitlines()
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        return tail[-max_chars:]
    return tail


def _looks_like_dump_root(path: Path) -> bool:
    return any((path / name).is_file() for name in _DUMP_MARKERS)


def _out_dir_has_foreign_content(out_dir: Path) -> bool:
    if not out_dir.exists():
        return False
    if not any(out_dir.iterdir()):
        return False
    return not _looks_like_dump_root(out_dir)


def system_ansi_encoding() -> str:
    """System ANSI code page: GetACP on Windows, locale elsewhere."""
    if os.name == "nt":
        try:
            import ctypes

            return f"cp{int(ctypes.windll.kernel32.GetACP())}"
        except Exception:
            pass
    return locale.getpreferredencoding(False) or "utf-8"


def params_file_encoding() -> str:
    """Encoding for 1C response file on this host (S1: utf-8-sig on Windows)."""
    if os.name != "nt":
        return "utf-8"
    return "utf-8-sig"


def _secure_temp_params_file(lines: Iterable[str], *, encoding: str | None = None) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="cfsmcp2-dump-", suffix=".txt")
    os.close(fd)
    path = Path(raw_path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    # Per-user temp; chmod on Windows does not restrict ACL (see CLI spec / reviewer M1).
    enc = encoding or params_file_encoding()
    path.write_text("\r\n".join(lines) + "\r\n", encoding=enc)
    return path


def _build_ib_connection_lines(profile: DumpProfileSpec) -> list[str]:
    lines: list[str] = ["DESIGNER"]
    if profile.ib_type == "file":
        lines.append(_param_line("/F", profile.ib_address))
    else:
        lines.append(_param_line("/S", profile.ib_address))
    if profile.ib_user:
        lines.append(_param_line("/N", profile.ib_user))
    if profile.password:
        lines.append(_param_line("/P", profile.password))
    lines.extend(["/DisableStartupDialogs", "/DisableStartupMessages"])
    return lines


def build_params_lines(profile: DumpProfileSpec, *, out_log_path: str | Path) -> list[str]:
    """Build response-file lines (its-metod8dev-2272-hdoc); password only here, never in argv."""
    lines = _build_ib_connection_lines(profile)
    lines.append(_build_dump_config_line(profile))
    lines.append(_param_line("/Out", str(out_log_path)))
    return lines


def build_list_extensions_params_lines(
    profile: DumpProfileSpec,
    *,
    out_log_path: str | Path,
) -> list[str]:
    """Configurator /DumpDBCfgList -AllExtensions (extension names in /Out, one per line)."""
    lines = _build_ib_connection_lines(profile)
    lines.append("/DumpDBCfgList -AllExtensions")
    lines.append(_param_line("/Out", str(out_log_path)))
    return lines


def parse_extension_list_output(text: str) -> list[str]:
    """Parse /Out from DumpDBCfgList -AllExtensions (v8runner: one extension name per line)."""
    names: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        name = raw.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


_LIST_EXTENSIONS_TIMEOUT_SEC = 120


def _validate_ib_connection(profile: DumpProfileSpec) -> str | None:
    if profile.ib_type == "file":
        if not profile.ib_address or not Path(profile.ib_address).exists():
            return "файловая информационная база не найдена"
    elif profile.ib_type == "server":
        if not profile.ib_address.strip():
            return "не указан адрес серверной информационной базы"
    else:
        return f"неизвестный тип ИБ: {profile.ib_type}"
    return None


def _run_params_file(
    profile: DumpProfileSpec,
    params_lines: list[str],
    *,
    out_log_path: str | Path,
    timeout_sec: int,
    params_encoding: str | None = None,
) -> tuple[int | None, str, str]:
    if not profile.platform_path or not Path(profile.platform_path).is_file():
        raise ValueError("не найден исполняемый файл платформы")
    log_path = Path(out_log_path)
    params_path = _secure_temp_params_file(params_lines, encoding=params_encoding)
    at_path = str(params_path)
    at_arg = f'@"{at_path}"' if " " in at_path else f"@{at_path}"
    argv = [profile.platform_path, at_arg]
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    proc: subprocess.Popen | None = None
    return_code: int | None = None
    log_text = ""
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        deadline = time.monotonic() + max(1, int(timeout_sec))
        while True:
            return_code = proc.poll()
            if return_code is not None:
                break
            if time.monotonic() >= deadline:
                _kill_process_tree(proc.pid)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    return None, "", "превышен таймаут"
                return_code = proc.returncode
                return return_code, "", "превышен таймаут"
            time.sleep(0.25)
        if log_path.is_file():
            try:
                log_text = _read_out_log(log_path)
            except OSError:
                log_text = ""
    finally:
        try:
            params_path.unlink(missing_ok=True)
        except OSError:
            log.debug("params file cleanup failed", exc_info=True)
        try:
            log_path.unlink(missing_ok=True)
        except OSError:
            pass
    return return_code, log_text, ""


def list_configuration_extensions(
    profile: DumpProfileSpec,
    *,
    timeout_sec: int = _LIST_EXTENSIONS_TIMEOUT_SEC,
    params_encoding: str | None = None,
) -> tuple[list[str], str]:
    ib_err = _validate_ib_connection(profile)
    if ib_err:
        return [], ib_err
    log_fd, log_raw = tempfile.mkstemp(prefix="cfsmcp2-ext-list-", suffix=".log")
    os.close(log_fd)
    out_log = Path(log_raw)
    try:
        return_code, log_text, timeout_err = _run_params_file(
            profile,
            build_list_extensions_params_lines(profile, out_log_path=out_log),
            out_log_path=out_log,
            timeout_sec=timeout_sec,
            params_encoding=params_encoding,
        )
    finally:
        try:
            out_log.unlink(missing_ok=True)
        except OSError:
            pass
    if timeout_err:
        return [], timeout_err
    if return_code not in (0, None):
        reason = classify_log_error(log_text) or _tail_text(log_text) or f"код возврата {return_code}"
        return [], reason
    return parse_extension_list_output(log_text), ""


def build_launch(
    profile: DumpProfileSpec,
    *,
    out_log_path: str | Path | None = None,
    params_encoding: str | None = None,
) -> _PreparedLaunch:
    """Prepare argv + temporary @ response file (``1cv8.exe @params.txt``)."""
    if not profile.platform_path:
        raise ValueError("platform_path is required")
    if out_log_path:
        log_path = Path(out_log_path)
    else:
        log_fd, log_raw = tempfile.mkstemp(prefix="cfsmcp2-dump-out-", suffix=".log")
        os.close(log_fd)
        log_path = Path(log_raw)
    params_path = _secure_temp_params_file(
        build_params_lines(profile, out_log_path=log_path),
        encoding=params_encoding,
    )
    at_path = str(params_path)
    at_arg = f'@"{at_path}"' if " " in at_path else f"@{at_path}"
    argv = [profile.platform_path, at_arg]
    return _PreparedLaunch(argv=argv, params_path=params_path, out_log_path=log_path)


def evaluate_verdict(
    return_code: int | None,
    log_text: str,
    out_dir: str | Path,
) -> tuple[bool, str]:
    """Success requires return code, clean log and Configuration.xml (design R2)."""
    cfg_exists = (Path(out_dir) / "Configuration.xml").is_file()
    reason = classify_log_error(log_text)
    if return_code not in (0, None):
        return False, reason or f"код возврата {return_code}"
    if reason:
        return False, reason
    if not cfg_exists:
        return False, "нет Configuration.xml в каталоге назначения"
    return True, ""


def compare_existing_dump(profile: DumpProfileSpec) -> list[str]:
    """Incremental mode: warn when existing dump likely belongs to another target."""
    warnings: list[str] = []
    out_dir = Path(profile.out_dir)
    if profile.dump_mode != "update" or not _looks_like_dump_root(out_dir):
        return warnings
    try:
        meta = read_configuration_meta(out_dir)
    except Exception as exc:
        warnings.append(f"не удалось прочитать Configuration.xml: {exc}")
        return warnings
    if profile.target_type == "extension":
        expected = profile.extension_name.strip()
        actual = meta.config_name.strip()
        if expected and actual and expected.lower() != actual.lower():
            warnings.append(
                f"в каталоге выгрузка расширения «{actual}», профиль ожидает «{expected}»"
            )
    else:
        if meta.entity_type == "extension":
            warnings.append("в каталоге выгрузка расширения, профиль — конфигурация")
    return warnings


def preflight_dump(profile: DumpProfileSpec) -> PreflightResult:
    """Engine-side checks before launch (design §4, decisions 21/23)."""
    errors: list[str] = []
    warnings: list[str] = []

    platform = Path(profile.platform_path)
    platform_info = validate_platform(profile.platform_path) if profile.platform_path else None
    if not profile.platform_path or not platform.is_file() or platform_info is None:
        errors.append("не найден исполняемый файл платформы")
    elif platform_info.version not in ("", "unknown"):
        if _parse_version_tuple(platform_info.version) < _MIN_HIERARCHICAL_PLATFORM:
            errors.append(
                f"платформа {platform_info.version} не поддерживает -format {DUMP_FORMAT}; "
                "обновите 1С:Предприятие"
            )
    if not profile.out_dir:
        errors.append("не указан каталог назначения")
    else:
        out_dir = Path(profile.out_dir)
        if out_dir.exists() and not out_dir.is_dir():
            errors.append("каталог назначения не является каталогом")
        else:
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                probe = out_dir / ".cfsmcp2_write_probe"
                probe.write_text("ok", encoding="ascii")
                probe.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"каталог назначения недоступен для записи: {exc}")

    if profile.ib_type == "file":
        if not profile.ib_address or not Path(profile.ib_address).exists():
            errors.append("файловая информационная база не найдена")
    elif profile.ib_type == "server":
        if not profile.ib_address.strip():
            errors.append("не указан адрес серверной информационной базы")
    else:
        errors.append(f"неизвестный тип ИБ: {profile.ib_type}")

    if profile.target_type == "extension" and not profile.extension_name.strip():
        errors.append("для расширения нужно имя расширения")

    if profile.dump_mode == "full" and profile.clear_before_full:
        out_dir = Path(profile.out_dir)
        if out_dir.exists() and _out_dir_has_foreign_content(out_dir):
            errors.append(
                "каталог содержит посторонние файлы; очистка перед полной выгрузкой запрещена"
            )
    elif profile.dump_mode == "full" and not profile.clear_before_full:
        out_dir = Path(profile.out_dir)
        if out_dir.exists() and _looks_like_dump_root(out_dir) and any(out_dir.iterdir()):
            warnings.append(
                "полная выгрузка без очистки не удаляет файлы объектов, исчезнувших из метаданных"
            )

    warnings.extend(compare_existing_dump(profile))
    return PreflightResult(ok=not errors, errors=errors, warnings=warnings)


def clear_out_directory_contents(out_dir: str | Path) -> None:
    """Remove dump directory contents (not the directory itself)."""
    root = Path(out_dir)
    if not root.is_dir():
        return
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=False)
        else:
            child.unlink(missing_ok=True)


def _needs_auto_bootstrap(profile: DumpProfileSpec) -> bool:
    if profile.dump_mode != "update":
        return False
    return not (Path(profile.out_dir) / "ConfigDumpInfo.xml").is_file()


def _kill_process_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        flags = CREATE_NO_WINDOW
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            check=False,
            creationflags=flags,
        )
    else:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass


def _execute_dump(
    profile: DumpProfileSpec,
    *,
    popen: Callable[..., subprocess.Popen] | None = None,
    cancel_event: threading.Event | None = None,
    params_encoding: str | None = None,
    progress_out_log: list[Path] | None = None,
) -> DumpRunResult:
    """Single Configurator invocation; always deletes the params file in ``finally``."""
    if profile.dump_mode == "full" and profile.clear_before_full:
        clear_out_directory_contents(profile.out_dir)

    prepared = build_launch(profile, params_encoding=params_encoding)
    if progress_out_log is not None:
        progress_out_log.clear()
        progress_out_log.append(prepared.out_log_path)
    proc: subprocess.Popen | None = None
    cancelled = False
    return_code: int | None = None
    log_text = ""

    popen_fn = popen or subprocess.Popen
    try:
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = popen_fn(
            prepared.argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        deadline = time.monotonic() + max(1, int(profile.timeout_sec))
        while True:
            if cancel_event and cancel_event.is_set():
                cancelled = True
                _kill_process_tree(proc.pid)
                break
            return_code = proc.poll()
            if return_code is not None:
                break
            if time.monotonic() >= deadline:
                _kill_process_tree(proc.pid)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    return DumpRunResult(
                        success=False,
                        return_code=proc.returncode,
                        reason="превышен таймаут; не удалось завершить процесс",
                        log_path=str(prepared.out_log_path),
                        cancelled=False,
                    )
                return_code = proc.returncode
                return DumpRunResult(
                    success=False,
                    return_code=return_code,
                    reason="превышен таймаут",
                    log_path=str(prepared.out_log_path),
                    cancelled=False,
                )
            time.sleep(0.25)
        if cancelled:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                return DumpRunResult(
                    success=False,
                    return_code=proc.returncode,
                    reason="отменено; не удалось завершить процесс",
                    log_path=str(prepared.out_log_path),
                    file_count=count_files_in_directory(profile.out_dir),
                    cancelled=True,
                )
            return_code = proc.returncode
        if prepared.out_log_path.is_file():
            try:
                log_text = _read_out_log(prepared.out_log_path)
            except OSError:
                log_text = ""
    finally:
        try:
            prepared.params_path.unlink(missing_ok=True)
        except OSError:
            log.debug("params file cleanup failed", exc_info=True)

    if cancelled:
        return DumpRunResult(
            success=False,
            return_code=return_code,
            reason="отменено",
            log_tail=_tail_text(log_text),
            log_path=str(prepared.out_log_path),
            file_count=count_files_in_directory(profile.out_dir),
            cancelled=True,
        )

    ok, reason = evaluate_verdict(return_code, log_text, profile.out_dir)
    return DumpRunResult(
        success=ok,
        return_code=return_code,
        reason=reason,
        log_tail=_tail_text(log_text),
        log_path=str(prepared.out_log_path),
        file_count=count_files_in_directory(profile.out_dir),
    )


def run_dump(
    profile: DumpProfileSpec,
    *,
    popen: Callable[..., subprocess.Popen] | None = None,
    cancel_event: threading.Event | None = None,
    params_encoding: str | None = None,
    progress_out_log: list[Path] | None = None,
) -> DumpRunResult:
    """Run Configurator dump with optional auto-bootstrap (stage 4 / S1)."""
    pre = preflight_dump(profile)
    if not pre.ok:
        return DumpRunResult(success=False, reason="; ".join(pre.errors))

    bootstrap_used = False
    if _needs_auto_bootstrap(profile):
        bootstrap_used = True
        boot_profile = replace(
            profile,
            dump_mode="full",
            clear_before_full=False,
        )
        boot_res = _execute_dump(
            boot_profile,
            popen=popen,
            cancel_event=cancel_event,
            params_encoding=params_encoding,
            progress_out_log=progress_out_log,
        )
        boot_res.bootstrap_used = True
        if not boot_res.success or boot_res.cancelled:
            return boot_res
        if cancel_event and cancel_event.is_set():
            boot_res.reason = "отменено"
            boot_res.cancelled = True
            return boot_res

    result = _execute_dump(
        profile,
        popen=popen,
        cancel_event=cancel_event,
        params_encoding=params_encoding,
        progress_out_log=progress_out_log,
    )
    result.bootstrap_used = bootstrap_used
    return result
