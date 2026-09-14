"""Windows portable GUI launcher: start/stop server, tray, GitHub update check."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from app.core.paths import default_data_dir, is_frozen, portable_exe_dir
from app.portable.instance_lock import LauncherInstanceLock
from app.portable.ui_theme import LauncherTheme
from app.portable.network_util import find_free_port, is_port_available, is_valid_http_url, local_ip
from app.portable.dump_window import DumpWindowController
from app.portable.ui_tooltip import Tooltip
from app.core.version import APP_VERSION
from app.portable.ini import (
    DEFAULT_PORT,
    LOCAL_HOST,
    NETWORK_HOST,
    PortableIni,
    is_network_bind,
    load_portable_ini,
    loopback_host,
    save_portable_ini,
)
log = logging.getLogger("cfsmcp2.launcher")

CREATE_NO_WINDOW = 0x08000000
STARTUP_TIMEOUT_SEC = 20


def _resource_path(name: str) -> Path:
    root = portable_exe_dir()
    for candidate in (root / name, root / "packaging" / "assets" / name):
        if candidate.is_file():
            return candidate
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / name
        if p.is_file():
            return p
    return root / name


def _server_executable(root: Path) -> tuple[list[str], Path | None]:
    """Return argv prefix and cwd for the server subprocess."""
    if is_frozen():
        return [str(Path(sys.executable)), "--server"], root
    repo_root = Path(__file__).resolve().parent.parent.parent
    return [sys.executable, "-m", "app.portable.launcher", "--server"], repo_root


def _build_server_env(ini: PortableIni) -> dict[str, str]:
    env = os.environ.copy()
    env["HOST"] = ini.host.strip() or "127.0.0.1"
    env["PORT"] = str(int(ini.port))
    env["LM_STUDIO_URL"] = ini.lm_studio_url.strip() or "http://127.0.0.1:1234"
    env["RUNTIME_MODE"] = "native"
    return env


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _health_ok(ini: PortableIni, timeout: float = 0.8) -> bool:
    import httpx

    host = loopback_host(ini.host)
    port = int(ini.port)
    url = f"http://{host}:{port}/api/health"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            return resp.status_code == 200
    except Exception:
        return False


class PortableLauncher:
    def __init__(self, instance_lock: LauncherInstanceLock | None = None) -> None:
        self.root_dir = portable_exe_dir()
        self.data_dir = default_data_dir()
        self.instance_name = self.root_dir.name
        self.instance_lock = instance_lock
        self.ini_path = self.root_dir / "cfsmcp2.ini"
        self.ini = PortableIni(host=LOCAL_HOST, port=DEFAULT_PORT)
        self.proc: subprocess.Popen | None = None
        self.tray_icon = None
        self.tray_thread: threading.Thread | None = None
        self._tray_icon_current: str | None = None
        self._closing = False
        self._ui_ready = False
        self._update_busy = False
        self._server_ready = False
        self._health_inflight = False
        self._health_poll_ms = 1500
        self._health_poll_ms_ready = 4000
        self._flash_after_id: str | None = None
        self._pending_crash_notice: int | None = None
        self._pending_tray_message: tuple[str, str, str] | None = None
        self._tray_tooltip_after_id: str | None = None
        self._start_attempt_at: float | None = None
        self._startup_timed_out = False
        self._advanced_visible = False
        self._update_window: tk.Toplevel | None = None
        self._update_progress_var: tk.DoubleVar | None = None
        self._update_status_var: tk.StringVar | None = None
        self._pending_update_info = None
        self._pending_update_staging: Path | None = None
        self._crash_dialog: tk.Toplevel | None = None
        self._status_state = "loading"
        self._status_base_text = "Загрузка…"
        self._status_flash_saved: tuple[str, str, str] | None = None
        self._cached_local_ip: str | None = None
        self._status_progress_running = False
        self._proc_started_at: float | None = None
        self._uptime_after_id: str | None = None
        self._last_crash_code: int | None = None
        self._status_extra_meta: str = ""
        self.theme = LauncherTheme
        self.dump_ctrl = DumpWindowController(self)

        self.root = tk.Tk()
        self.root.title(f"cfsmcp2 — {self.instance_name}")
        self.root.resizable(False, True)
        self.root.configure(bg=self.theme.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_window)
        self.root.bind("<Unmap>", self._on_root_unmap)
        self.style = self.theme.apply_ttk(self.root)

        self.body = ttk.Frame(self.root, style="Root.TFrame", padding=(0, 0, 0, self.theme.sp(12)))
        self.body.pack(fill=tk.BOTH, expand=True)
        self._build_header_shell()
        self.root.update_idletasks()
        self.root.update()
        self.root.after(0, self._finish_init)

    def _build_header_shell(self) -> None:
        accent = tk.Frame(self.body, bg=self.theme.ACCENT, height=self.theme.sp(3))
        accent.pack(fill=tk.X)
        header = ttk.Frame(
            self.body,
            style="Header.TFrame",
            padding=(self.theme.sp(16), self.theme.sp(10)),
        )
        header.pack(fill=tk.X)

        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        title_row = tk.Frame(left, bg=self.theme.SURFACE)
        title_row.pack(anchor="w")
        self.brand_label = ttk.Label(title_row, text="cfsmcp2", style="HeaderTitle.TLabel", cursor="hand2")
        self.brand_label.pack(side=tk.LEFT)
        self.brand_label.bind("<Button-1>", lambda _e: self._open_github_home())
        self.brand_label.bind("<Enter>", lambda _e: self.brand_label.configure(foreground=self.theme.INFO), add="+")
        self.brand_label.bind("<Leave>", lambda _e: self.brand_label.configure(foreground=self.theme.FG), add="+")
        ttk.Label(left, text="Портативный запуск", style="HeaderMuted.TLabel").pack(anchor="w", pady=(self.theme.sp(2), 0))

        right = ttk.Frame(header, style="Header.TFrame")
        right.pack(side=tk.RIGHT)
        version_row = tk.Frame(right, bg=self.theme.SURFACE)
        version_row.pack(anchor="e")
        self.version_badge = tk.Label(
            version_row,
            text=f"v{APP_VERSION}",
            font=("Segoe UI", 9, "bold"),
            fg=self.theme.ACCENT,
            bg=self.theme.SURFACE,
            padx=self.theme.sp(8),
            pady=self.theme.sp(3),
            highlightthickness=1,
            highlightbackground=self.theme.CARD_BORDER,
        )
        self.version_badge.pack(side=tk.LEFT)
        self.kebab_btn = tk.Label(
            version_row,
            text="⋯",
            font=("Segoe UI", 14, "bold"),
            fg=self.theme.MUTED,
            bg=self.theme.SURFACE,
            cursor="hand2",
            padx=self.theme.sp(4),
            takefocus=1,
            highlightthickness=1,
            highlightbackground=self.theme.SURFACE,
            highlightcolor=self.theme.INFO,
        )
        self.kebab_btn.pack(side=tk.LEFT, padx=(self.theme.sp(6), 0))
        self._wire_focusable_label(self.kebab_btn, self._show_kebab_menu)
        self.kebab_btn.bind("<Enter>", lambda _e: self.kebab_btn.configure(fg=self.theme.FG), add="+")
        self.kebab_btn.bind("<Leave>", lambda _e: self.kebab_btn.configure(fg=self.theme.MUTED), add="+")
        Tooltip(
            self.kebab_btn,
            "Меню",
            bg=self.theme.INPUT_BG,
            fg=self.theme.FG,
            border=self.theme.CARD_BORDER,
        )
        self.content = ttk.Frame(self.body, style="Root.TFrame")
        self.content.pack(fill=tk.BOTH, expand=True)
        self._build_status_band()

    def _build_status_band(self) -> None:
        band_h = self.theme.sp(36)
        self.status_band = tk.Frame(self.body, bg=self.theme.STATUS_LOADING_BG, height=band_h)
        self.status_band.pack(fill=tk.X, before=self.content)
        self.status_band.pack_propagate(False)
        inner = tk.Frame(self.status_band, bg=self.theme.STATUS_LOADING_BG, padx=self.theme.sp(16))
        inner.pack(fill=tk.BOTH, expand=True)

        self.status_band_inner = inner
        left = tk.Frame(inner, bg=self.theme.STATUS_LOADING_BG)
        self.status_band_left = left
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        dot_size = self.theme.sp(10)
        self.status_dot = tk.Canvas(
            left,
            width=dot_size,
            height=dot_size,
            bg=self.theme.STATUS_LOADING_BG,
            highlightthickness=0,
        )
        self.status_dot.pack(side=tk.LEFT, padx=(0, self.theme.sp(8)))
        self._status_dot_inset = max(2, self.theme.sp(2))
        self._status_dot_size = dot_size
        self.status_text_var = tk.StringVar(value="Загрузка…")
        self.status_text_label = tk.Label(
            left,
            textvariable=self.status_text_var,
            font=("Segoe UI", 10, "bold"),
            fg=self.theme.FG,
            bg=self.theme.STATUS_LOADING_BG,
            anchor="w",
        )
        self.status_text_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.status_progress = ttk.Progressbar(
            inner,
            mode="indeterminate",
            length=self.theme.sp(84),
            style="Status.Horizontal.TProgressbar",
        )
        self.style.configure(
            "Status.Horizontal.TProgressbar",
            troughcolor=self.theme.INPUT_BG,
            background=self.theme.WARNING,
            bordercolor=self.theme.INPUT_BG,
            lightcolor=self.theme.WARNING,
            darkcolor=self.theme.WARNING,
        )
        self.status_band_action = tk.Label(
            inner,
            text="",
            font=self.theme.font_sm(),
            fg=self.theme.INFO,
            bg=self.theme.STATUS_LOADING_BG,
            cursor="hand2",
        )
        self.status_band_action.bind("<Button-1>", lambda _e: self._open_server_log())

        for widget in (self.status_band, inner, left, self.status_text_label):
            widget.bind("<Button-1>", self._on_status_band_click, add="+")
        self._set_status_band("loading", "Загрузка…")

    def _status_band_bg(self, state: str) -> str:
        return {
            "loading": self.theme.STATUS_LOADING_BG,
            "stopped": self.theme.STATUS_STOPPED_BG,
            "running": self.theme.STATUS_RUNNING_BG,
            "unresponsive": self.theme.STATUS_UNRESPONSIVE_BG,
            "error": self.theme.STATUS_ERROR_BG,
            "flash": self.theme.STATUS_FLASH_BG,
        }.get(state, self.theme.STATUS_STOPPED_BG)

    def _status_dot_color(self, state: str) -> str:
        return {
            "loading": self.theme.WARNING,
            "stopped": self.theme.MUTED,
            "running": self.theme.ACCENT,
            "unresponsive": self.theme.WARNING,
            "error": self.theme.DANGER,
            "flash": self.theme.INFO,
        }.get(state, self.theme.MUTED)

    def _format_uptime(self, seconds: int) -> str:
        seconds = max(0, seconds)
        hours, rem = divmod(seconds, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            if minutes:
                return f"{hours} ч {minutes} мин"
            return f"{hours} ч"
        if minutes:
            return f"{minutes} мин"
        return f"{secs} с"

    def _format_status_line(self, base: str) -> str:
        parts = [base]
        if self._status_extra_meta:
            parts.append(self._status_extra_meta)
        elif self._ui_ready:
            try:
                parts.append(f"порт {self._current_port()}")
            except ValueError:
                parts.append(f"порт {self.ini.port}")
        if self._server_ready and self._proc_started_at is not None:
            uptime = int(time.monotonic() - self._proc_started_at)
            parts.append(self._format_uptime(uptime))
        return " · ".join(parts)

    def _paint_status_dot(self, state: str, bg: str) -> None:
        color = self._status_dot_color(state)
        self.status_dot.configure(bg=bg)
        self.status_dot.delete("all")
        inset = getattr(self, "_status_dot_inset", 2)
        outer = getattr(self, "_status_dot_size", 10) - inset
        self.status_dot.create_oval(inset, inset, outer, outer, fill=color, outline=color)

    def _sync_status_progress(self) -> None:
        show = (
            self._status_state == "loading"
            and self._server_process_alive()
            and not self._server_ready
            and self._status_flash_saved is None
        )
        if show:
            self._hide_status_band_action()
            if not self._status_progress_running:
                self.status_progress.pack(side=tk.RIGHT)
                self.status_progress.start(12)
                self._status_progress_running = True
            return
        if self._status_progress_running:
            self.status_progress.stop()
            self.status_progress.pack_forget()
            self._status_progress_running = False

    def _sync_status_band_action(self) -> None:
        if not hasattr(self, "status_band_action"):
            return
        if self._status_state in ("unresponsive", "error"):
            bg = self._status_band_bg(self._status_state)
            self.status_band_action.configure(text="Показать лог", bg=bg)
            if not self.status_band_action.winfo_ismapped():
                self.status_band_action.pack(side=tk.RIGHT)
            return
        self._hide_status_band_action()

    def _hide_status_band_action(self) -> None:
        if hasattr(self, "status_band_action"):
            self.status_band_action.pack_forget()

    def _set_status_band(
        self,
        state: str,
        base_text: str,
        *,
        force: bool = False,
        extra_meta: str = "",
    ) -> None:
        if self._update_busy and not force:
            return
        if state != "flash":
            self._status_state = state
            self._status_base_text = base_text
            self._status_extra_meta = extra_meta
        bg = self._status_band_bg(state)
        for widget in (
            self.status_band,
            getattr(self, "status_band_inner", None),
            getattr(self, "status_band_left", None),
            self.status_text_label,
            self.status_dot,
        ):
            if widget is not None:
                try:
                    widget.configure(bg=bg)
                except tk.TclError:
                    pass
        line = self._format_status_line(base_text)
        if self.status_text_var.get() != line:
            self.status_text_var.set(line)
        self._paint_status_dot(state if state != "flash" else "flash", bg)
        self._sync_status_progress()
        self._sync_status_band_action()
        if state != "flash":
            self._update_tray_icon(state)

    def _tray_icon_state(self, band_state: str | None = None) -> str:
        state = band_state or self._status_state
        if state == "running":
            return "running"
        if state == "error":
            return "error"
        if state in ("loading", "unresponsive"):
            return "loading"
        return "stopped"

    def _update_tray_icon(self, band_state: str) -> None:
        if self.tray_icon is None:
            return
        state = self._tray_icon_state(band_state)
        if state == self._tray_icon_current:
            return
        try:
            from app.portable.tray_icon import load_tray_image

            self.tray_icon.icon = load_tray_image(
                _resource_path("cfs-mark.ico"),
                state=state,
            )
            self._tray_icon_current = state
        except Exception:
            log.debug("tray icon update failed", exc_info=True)

    def _restore_status_band(self) -> None:
        if self._status_flash_saved is not None:
            state, base, extra = self._status_flash_saved
            self._status_flash_saved = None
            self._set_status_band(state, base, force=True, extra_meta=extra)
            return
        if self._update_busy:
            return
        if self.proc and self.proc.poll() is None:
            if self._server_ready:
                self._set_status_band("running", "Запущен")
            elif self._startup_timed_out:
                self._set_status_band("unresponsive", "Не отвечает")
            else:
                self._set_status_band("loading", "Запуск…")
        elif self._last_crash_code is not None:
            self._set_crash_status_band(self._last_crash_code)
        else:
            self._set_status_band("stopped", "Остановлен")

    def _set_crash_status_band(self, code: int) -> None:
        self._set_status_band("error", "Сервер завершился", extra_meta=f"код {code}")

    def _on_status_band_click(self, event: tk.Event | None = None) -> None:
        if event is not None and event.widget is getattr(self, "status_band_action", None):
            return
        if self._status_state in ("unresponsive", "error"):
            self._open_server_log()

    def _schedule_uptime_tick(self) -> None:
        if self._uptime_after_id is not None:
            try:
                self.root.after_cancel(self._uptime_after_id)
            except tk.TclError:
                pass
            self._uptime_after_id = None
        if not self._server_ready or self._proc_started_at is None:
            return

        def _tick() -> None:
            self._uptime_after_id = None
            if not self._server_ready or self._proc_started_at is None:
                return
            if self._status_state in ("running",) and not self._update_busy:
                self.status_text_var.set(self._format_status_line(self._status_base_text))
            self._uptime_after_id = self.root.after(1000, _tick)

        self._uptime_after_id = self.root.after(1000, _tick)

    def _dismiss_first_run_hint(self) -> None:
        if not hasattr(self, "first_run_label"):
            return
        try:
            self.first_run_label.pack_forget()
        except tk.TclError:
            pass
        if not self.ini.first_run_hint_shown:
            self.ini.first_run_hint_shown = True
            save_portable_ini(self.ini_path, self.ini)
        self._grow_to_fit()

    def _refresh_local_ip_cache(self) -> None:
        self._cached_local_ip = local_ip()

    def _make_bind_option(
        self,
        parent: tk.Frame,
        *,
        value: str,
        title: str,
        subtitle: str,
    ) -> ttk.Radiobutton:
        row = tk.Frame(parent, bg=self.theme.CARD)
        row.pack(fill=tk.X, pady=(0, self.theme.sp(6)))
        rb = ttk.Radiobutton(
            row,
            value=value,
            variable=self.bind_mode_var,
            command=self._on_network_settings_changed,
            state=tk.DISABLED,
        )
        rb.pack(side=tk.LEFT, anchor="n", padx=(0, self.theme.sp(4)))
        text_col = tk.Frame(row, bg=self.theme.CARD)
        text_col.pack(side=tk.LEFT, fill=tk.X, expand=True)
        title_l = tk.Label(
            text_col,
            text=title,
            font=self.theme.font_body(),
            fg=self.theme.FG,
            bg=self.theme.CARD,
            anchor="w",
        )
        title_l.pack(anchor="w")
        sub_l = tk.Label(
            text_col,
            text=subtitle,
            font=self.theme.font_sm(),
            fg=self.theme.MUTED,
            bg=self.theme.CARD,
            anchor="w",
            wraplength=self.theme.sp(300),
            justify=tk.LEFT,
        )
        sub_l.pack(anchor="w")
        self._bind_wraplength(sub_l, text_col, self.theme.sp(300))

        def _select(_event: tk.Event | None = None) -> None:
            if str(rb["state"]) == tk.NORMAL:
                self.bind_mode_var.set(value)
                self._on_network_settings_changed()

        for widget in (row, text_col, title_l, sub_l):
            widget.bind("<Button-1>", _select)
        return rb

    def _build_instance_footer(self, parent: tk.Misc) -> None:
        self.instance_footer = tk.Frame(parent, bg=self.theme.BG)
        self.instance_footer.pack(fill=tk.X, padx=self.theme.sp(16), pady=(self.theme.sp(8), 0))
        row = tk.Frame(self.instance_footer, bg=self.theme.BG)
        row.pack(fill=tk.X)
        left = tk.Frame(row, bg=self.theme.BG)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            left,
            text="cfsmcp2",
            font=self.theme.font_mono(),
            fg=self.theme.BTN_DISABLED_FG,
            bg=self.theme.BG,
        ).pack(side=tk.LEFT)
        tk.Label(left, text=" · ", font=self.theme.font_mono(), fg=self.theme.BTN_DISABLED_FG, bg=self.theme.BG).pack(
            side=tk.LEFT
        )
        data_label = tk.Label(
            left,
            text=str(self.data_dir),
            font=self.theme.font_mono(),
            fg=self.theme.BTN_DISABLED_FG,
            bg=self.theme.BG,
            cursor="hand2",
            anchor="w",
        )
        data_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        data_label.bind("<Button-1>", lambda _e: self._open_data_dir())
        data_label.bind("<Enter>", lambda _e: data_label.configure(fg=self.theme.INFO), add="+")
        data_label.bind("<Leave>", lambda _e: data_label.configure(fg=self.theme.BTN_DISABLED_FG), add="+")
        Tooltip(
            data_label,
            "Открыть папку данных",
            bg=self.theme.INPUT_BG,
            fg=self.theme.FG,
            border=self.theme.CARD_BORDER,
        )
        log_label = tk.Label(
            row,
            text="лог",
            font=self.theme.font_mono(),
            fg=self.theme.INFO,
            bg=self.theme.BG,
            cursor="hand2",
        )
        log_label.pack(side=tk.RIGHT)
        log_label.bind("<Button-1>", lambda _e: self._open_server_log())
        Tooltip(
            log_label,
            "Показать server.log",
            bg=self.theme.INPUT_BG,
            fg=self.theme.FG,
            border=self.theme.CARD_BORDER,
        )

    def _finish_init(self) -> None:
        self.ini = load_portable_ini(self.ini_path)
        self._set_window_icon()

        _, access_card = self.theme.card(self.content)
        self.theme.section_title(access_card, "Доступ").pack(anchor="w", pady=(0, 8))

        self.bind_mode_var = tk.StringVar(
            value="network" if is_network_bind(self.ini.host) else "local"
        )
        self.bind_local_rb = self._make_bind_option(
            access_card,
            value="local",
            title="Только этот компьютер",
            subtitle="127.0.0.1 — никто извне не подключится",
        )
        self.bind_network_rb = self._make_bind_option(
            access_card,
            value="network",
            title="Доступ по сети",
            subtitle="0.0.0.0 — нужен открытый порт в брандмауэре",
        )

        port_row = tk.Frame(access_card, bg=self.theme.CARD)
        port_row.pack(fill=tk.X, pady=(10, 4))
        tk.Label(port_row, text="Порт", font=self.theme.font_body(), fg=self.theme.MUTED, bg=self.theme.CARD).pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(self.ini.port))
        vcmd = (self.root.register(self._validate_port_input), "%P")
        self.port_entry = ttk.Entry(
            port_row,
            textvariable=self.port_var,
            width=10,
            state=tk.DISABLED,
            validate="key",
            validatecommand=vcmd,
        )
        self.port_entry.pack(side=tk.LEFT, padx=(10, 0))
        self.port_entry.bind("<FocusOut>", lambda _e: self._save_network_settings())
        self.port_entry.bind("<Return>", lambda _e: self._save_network_settings() or "break")
        self.port_var.trace_add("write", lambda *_: self._on_port_field_changed())
        self.pick_port_btn = self.theme.make_button(port_row, "Свободный", self._pick_free_port)
        self.port_error_var = tk.StringVar(value="")
        tk.Label(
            access_card,
            textvariable=self.port_error_var,
            font=self.theme.font_sm(),
            fg=self.theme.DANGER,
            bg=self.theme.CARD,
            anchor="w",
        ).pack(fill=tk.X, pady=(2, 0))
        tk.Frame(access_card, bg=self.theme.CARD_BORDER, height=1).pack(fill=tk.X, pady=(12, 10))

        self.ui_url_var = tk.StringVar(value=self._ui_url())
        self.mcp_url_var = tk.StringVar(value=self._mcp_url())

        url_grid = tk.Frame(access_card, bg=self.theme.CARD)
        url_grid.pack(fill=tk.X)
        url_grid.columnconfigure(1, weight=1)

        tk.Label(url_grid, text="UI", font=self.theme.font_sm(), fg=self.theme.MUTED, bg=self.theme.CARD, width=5, anchor="w").grid(
            row=0, column=0, sticky="nw", pady=3
        )
        ui_row = tk.Frame(url_grid, bg=self.theme.CARD)
        ui_row.grid(row=0, column=1, sticky="ew", pady=3)
        ui_row.columnconfigure(0, weight=1)
        self.ui_url_entry = self._make_url_entry(ui_row)
        self.ui_url_entry.grid(row=0, column=0, sticky="ew")
        ui_btns = tk.Frame(ui_row, bg=self.theme.CARD)
        ui_btns.grid(row=0, column=1, sticky="e")
        self.ui_copy_btn = self._make_icon_btn(
            ui_btns,
            "⧉",
            lambda: self._copy_with_flash(self._ui_url()),
            tooltip="Скопировать",
        )

        tk.Label(url_grid, text="MCP", font=self.theme.font_sm(), fg=self.theme.MUTED, bg=self.theme.CARD, width=5, anchor="w").grid(
            row=1, column=0, sticky="nw", pady=3
        )
        mcp_row = tk.Frame(url_grid, bg=self.theme.CARD)
        mcp_row.grid(row=1, column=1, sticky="ew", pady=3)
        mcp_row.columnconfigure(0, weight=1)
        self.mcp_url_entry = self._make_url_entry(mcp_row)
        self.mcp_url_entry.grid(row=0, column=0, sticky="ew")
        mcp_btns = tk.Frame(mcp_row, bg=self.theme.CARD)
        mcp_btns.grid(row=0, column=1, sticky="e")
        self.mcp_copy_btn = self._make_icon_btn(
            mcp_btns,
            "⧉",
            lambda: self._copy_with_flash(self._mcp_url()),
            tooltip="Скопировать",
        )
        self._set_url_entry(self.ui_url_entry, self.ui_url_var.get())
        self._set_url_entry(self.mcp_url_entry, self.mcp_url_var.get())

        self.network_url_row = tk.Frame(url_grid, bg=self.theme.CARD)
        self.network_url_label = tk.Label(
            self.network_url_row,
            text="в сети",
            font=self.theme.font_sm(),
            fg=self.theme.ACCENT,
            bg=self.theme.CARD,
            width=5,
            anchor="w",
        )
        self.network_url_label.pack(side=tk.LEFT, anchor="nw", pady=3)
        net_inner = tk.Frame(self.network_url_row, bg=self.theme.CARD)
        net_inner.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.network_mcp_url_var = tk.StringVar(value="")
        self.network_mcp_url_entry = self._make_url_entry(net_inner)
        self.network_mcp_url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        net_btns = tk.Frame(net_inner, bg=self.theme.CARD)
        net_btns.pack(side=tk.RIGHT)
        self.network_mcp_copy_btn = self._make_icon_btn(
            net_btns,
            "⧉",
            lambda: self._copy_with_flash(self._network_mcp_url()),
            tooltip="Скопировать",
        )

        _, advanced_card = self.theme.card(self.content)
        self.advanced_header = tk.Frame(advanced_card, bg=self.theme.CARD, cursor="hand2")
        self.advanced_header.pack(fill=tk.X)
        self.advanced_title_var = tk.StringVar(value="Дополнительно  ▾")
        self.advanced_title_label = tk.Label(
            self.advanced_header,
            textvariable=self.advanced_title_var,
            font=self.theme.font_sm("bold"),
            fg=self.theme.MUTED,
            bg=self.theme.CARD,
            anchor="w",
            cursor="hand2",
        )
        self.advanced_title_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        for widget in (self.advanced_header, self.advanced_title_label):
            widget.bind("<Button-1>", lambda _e: self._toggle_advanced())
        self.advanced_body = tk.Frame(advanced_card, bg=self.theme.CARD)

        lm_row = tk.Frame(self.advanced_body, bg=self.theme.CARD)
        lm_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(lm_row, text="LM Studio URL", font=self.theme.font_sm(), fg=self.theme.MUTED, bg=self.theme.CARD).pack(
            anchor="w"
        )
        self.lm_studio_var = tk.StringVar(value=self.ini.lm_studio_url)
        self.lm_studio_entry = ttk.Entry(lm_row, textvariable=self.lm_studio_var, width=42)
        self.lm_studio_entry.pack(fill=tk.X, pady=(4, 0))
        self.lm_studio_entry.bind("<FocusOut>", lambda _e: self._save_advanced_settings())
        self.lm_studio_entry.bind("<Return>", lambda _e: self._save_advanced_settings() or "break")
        self.lm_studio_error_var = tk.StringVar(value="")
        tk.Label(
            self.advanced_body,
            textvariable=self.lm_studio_error_var,
            font=self.theme.font_sm(),
            fg=self.theme.DANGER,
            bg=self.theme.CARD,
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 4))

        self.minimize_tray_var = tk.BooleanVar(value=self.ini.minimize_to_tray)
        self.theme.make_checkbutton(
            self.advanced_body,
            "Сворачивать в трей при закрытии окна",
            self.minimize_tray_var,
            self._save_advanced_settings,
        ).pack(anchor="w", pady=2)
        self.autostart_server_var = tk.BooleanVar(value=self.ini.autostart_server)
        self.theme.make_checkbutton(
            self.advanced_body,
            "Запускать сервер при старте лаунчера",
            self.autostart_server_var,
            self._save_advanced_settings,
        ).pack(anchor="w", pady=2)

        foot = tk.Frame(self.body, bg=self.theme.BG, padx=self.theme.sp(self.theme.PAD_X))
        foot.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(foot, bg=self.theme.CARD_BORDER, height=1).pack(fill=tk.X, pady=(self.theme.sp(10), self.theme.sp(10)))
        btn_row = tk.Frame(foot, bg=self.theme.BG)
        btn_row.pack(fill=tk.X)
        btn_row.columnconfigure(0, weight=self.theme.BTN_PRIMARY_WEIGHT, uniform="actions")
        btn_row.columnconfigure(1, weight=self.theme.BTN_SECONDARY_WEIGHT, uniform="actions")

        self.primary_btn = self.theme.make_button(btn_row, "Запустить", self.start_server, kind="primary")
        self.primary_btn.grid(row=0, column=0, sticky="ew", padx=(0, self.theme.sp(4)))
        self.theme.set_button_enabled(self.primary_btn, False)

        self.secondary_btn = self.theme.make_button(btn_row, "Открыть UI", self.open_ui, kind="footer")
        self.secondary_btn.grid(row=0, column=1, sticky="ew", padx=(self.theme.sp(4), 0))
        self.theme.set_button_enabled(self.secondary_btn, False)

        if not self.ini.first_run_hint_shown:
            self.first_run_label = tk.Label(
                foot,
                text="Нажмите «Запустить», затем «Открыть UI»",
                font=self.theme.font_sm(),
                fg=self.theme.MUTED,
                bg=self.theme.BG,
                anchor="center",
                justify=tk.CENTER,
            )
            self.first_run_label.pack(fill=tk.X, pady=(self.theme.sp(8), 0))

        self._build_instance_footer(foot)

        self.root.bind("<Return>", self._on_enter_key, add="+")
        self.root.bind("<Escape>", lambda _e: self._hide_to_tray(), add="+")
        self.root.bind("<F5>", lambda _e: self.check_update(), add="+")

        self._bind_version_update()
        self._build_kebab_menu()

        self._refresh_local_ip_cache()
        self._ui_ready = True
        self._set_running(False)
        self._compact_window()
        self._ensure_tray()
        self._refresh_network_url_row()
        self.root.after(1000, self._poll_server)
        if self.ini.autostart_server:
            self.root.after(800, self.start_server)

    def _compact_window(self) -> None:
        self.root.update_idletasks()
        self.theme.set_scale(self.root)
        w = self.theme.sp(self.theme.WIN_WIDTH)
        h = max(self.root.winfo_reqheight(), self.theme.sp(self.theme.WIN_MIN_HEIGHT))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(w, h)

    def _grow_to_fit(self) -> None:
        self.root.update_idletasks()
        w = self.root.winfo_width()
        need = max(self.root.winfo_reqheight(), self.theme.sp(self.theme.WIN_MIN_HEIGHT))
        self.root.geometry(f"{w}x{need}")
        self.root.minsize(w, need)

    def _set_window_icon(self) -> None:
        ico = _resource_path("cfs-mark.ico")
        if ico.is_file():
            try:
                self.root.iconbitmap(str(ico))
            except tk.TclError:
                pass

    def _host_from_ui(self) -> str:
        return NETWORK_HOST if self.bind_mode_var.get() == "network" else LOCAL_HOST

    def _port_from_ui(self) -> int:
        text = self.port_var.get().strip()
        if not text.isdigit():
            raise ValueError("port must be 1..65535")
        port = int(text)
        if port < 1 or port > 65535:
            raise ValueError("port must be 1..65535")
        return port

    def _current_port(self) -> int:
        try:
            return self._port_from_ui()
        except ValueError:
            return int(self.ini.port)

    def _ui_url(self) -> str:
        host = loopback_host(self._host_from_ui())
        return f"http://{host}:{self._current_port()}/"

    def _mcp_url(self) -> str:
        host = loopback_host(self._host_from_ui())
        return f"http://{host}:{self._current_port()}/mcp/"

    def _network_mcp_url(self) -> str:
        if not is_network_bind(self._host_from_ui()):
            return ""
        ip = self._cached_local_ip or local_ip()
        return f"http://{ip}:{self._current_port()}/mcp/"

    def _refresh_network_url_row(self) -> None:
        if not hasattr(self, "network_url_row"):
            return
        if is_network_bind(self._host_from_ui()):
            url = self._network_mcp_url()
            self.network_mcp_url_var.set(url)
            self._set_url_entry(self.network_mcp_url_entry, url)
            self.network_url_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(self.theme.sp(4), 0))
        else:
            self.network_url_row.grid_remove()

    def _refresh_url_fields(self) -> None:
        ui = self._ui_url()
        mcp = self._mcp_url()
        self.ui_url_var.set(ui)
        self.mcp_url_var.set(mcp)
        if hasattr(self, "ui_url_entry"):
            self._set_url_entry(self.ui_url_entry, ui)
            self._set_url_entry(self.mcp_url_entry, mcp)
        self._refresh_network_url_row()

    def _bind_wraplength(self, label: tk.Label, parent: tk.Misc, fallback: int) -> None:
        def _update(_event: tk.Event | None = None) -> None:
            try:
                width = parent.winfo_width()
                if width > 40:
                    new_len = max(fallback, width - self.theme.sp(40))
                    if label.cget("wraplength") != new_len:
                        label.configure(wraplength=new_len)
            except tk.TclError:
                pass

        parent.bind("<Configure>", _update, add="+")

    def _wire_focusable_label(self, widget: tk.Label, command) -> None:
        def _activate(_event: tk.Event | None = None) -> None:
            command()

        widget.bind("<Button-1>", lambda _e: command())
        widget.bind("<Return>", _activate, add="+")
        widget.bind("<space>", _activate, add="+")

    def _make_url_entry(self, parent: tk.Misc) -> tk.Entry:
        return tk.Entry(
            parent,
            font=self.theme.font_mono(),
            fg=self.theme.FG,
            bg=self.theme.CARD,
            readonlybackground=self.theme.CARD,
            selectbackground=self.theme.INFO,
            selectforeground=self.theme.BG,
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            cursor="xterm",
            state="readonly",
        )

    def _set_url_entry(self, entry: tk.Entry, text: str) -> None:
        entry.config(state="normal")
        entry.delete(0, tk.END)
        entry.insert(0, text)
        entry.config(state="readonly")

    def _make_icon_btn(
        self,
        parent: tk.Misc,
        glyph: str,
        command,
        *,
        enabled: bool = True,
        tooltip: str = "",
        disabled_tooltip: str = "",
    ) -> tk.Label:
        btn = tk.Label(
            parent,
            text=glyph,
            font=self.theme.font_sm(),
            fg=self.theme.MUTED,
            bg=self.theme.CARD,
            cursor="arrow",
            padx=self.theme.sp(4),
            takefocus=1,
            highlightthickness=1,
            highlightbackground=self.theme.CARD,
            highlightcolor=self.theme.INFO,
        )
        btn.pack(side=tk.LEFT)
        btn._tooltip_enabled = tooltip  # type: ignore[attr-defined]
        btn._tooltip_disabled = disabled_tooltip or tooltip  # type: ignore[attr-defined]
        if tooltip or disabled_tooltip:
            btn._tooltip = Tooltip(  # type: ignore[attr-defined]
                btn,
                tooltip if enabled else (disabled_tooltip or tooltip),
                bg=self.theme.INPUT_BG,
                fg=self.theme.FG,
                border=self.theme.CARD_BORDER,
            )
        self._set_icon_btn_enabled(btn, enabled, command if enabled else None)
        return btn

    def _set_icon_btn_enabled(self, btn: tk.Label, enabled: bool, command) -> None:
        for seq in ("<Button-1>", "<Return>", "<space>"):
            btn.unbind(seq)
        if enabled and command is not None:
            btn.configure(fg=self.theme.INFO, cursor="hand2")
            self._wire_focusable_label(btn, command)
            tip = getattr(btn, "_tooltip", None)
            if tip is not None:
                tip.set_text(getattr(btn, "_tooltip_enabled", ""))
        else:
            btn.configure(fg=self.theme.MUTED, cursor="arrow")
            tip = getattr(btn, "_tooltip", None)
            if tip is not None:
                tip.set_text(getattr(btn, "_tooltip_disabled", ""))

    def _copy_text(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def _copy_with_flash(self, text: str, message: str = "URL скопирован") -> None:
        if not self._ui_ready:
            return
        self._copy_text(text)
        self._show_flash(message)

    def _show_flash(self, message: str, ms: int = 2000) -> None:
        if self._flash_after_id is not None:
            try:
                self.root.after_cancel(self._flash_after_id)
            except tk.TclError:
                pass
        if self._status_flash_saved is None:
            self._status_flash_saved = (
                self._status_state,
                self._status_base_text,
                self._status_extra_meta,
            )
        self._set_status_band("flash", message, force=True)
        self._flash_after_id = self.root.after(ms, self._clear_flash)

    def _clear_flash(self) -> None:
        self._flash_after_id = None
        self._restore_status_band()

    def _on_enter_key(self, _event: tk.Event | None = None) -> str | None:
        if not self._ui_ready:
            return "break"
        focus = self.root.focus_get()
        if focus is not None and focus not in (self.root, self.primary_btn, self.secondary_btn):
            return None
        for btn in (self.primary_btn, self.secondary_btn):
            if str(btn["state"]) == tk.NORMAL:
                btn.invoke()
                return "break"
        return "break"

    def _open_path(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            if os.name == "nt" and path.is_file():
                try:
                    subprocess.Popen(["notepad.exe", str(path)], creationflags=CREATE_NO_WINDOW)
                except OSError:
                    self._show_error("cfsmcp2", f"Не удалось открыть:\n{path}")
            else:
                self._show_error("cfsmcp2", f"Не удалось открыть:\n{path}")

    def _open_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._open_path(self.data_dir)

    def _server_log_path(self) -> Path:
        return self.data_dir / "server.log"

    def _newest_log_path(self) -> Path | None:
        candidates = [self._server_log_path(), self.data_dir / "crash.log"]
        existing = [path for path in candidates if path.is_file()]
        if not existing:
            return None
        return max(existing, key=lambda path: path.stat().st_mtime)

    def _open_server_log(self) -> None:
        path = self._newest_log_path()
        if path is not None:
            self._open_path(path)
            return
        self._show_info("cfsmcp2", "Лог пока не создан (data\\server.log).")

    def _open_ini_file(self) -> None:
        if not self.ini_path.is_file():
            save_portable_ini(self.ini_path, self.ini)
        self._open_path(self.ini_path)

    def _show_about(self) -> None:
        self._show_info(
            "О программе",
            f"Версия {APP_VERSION}\n\n"
            f"Каталог: {self.root_dir}\n"
            f"Данные: {self.data_dir}",
        )

    def _build_kebab_menu(self) -> None:
        self._kebab_menu = self.theme.popup_menu(self.root)
        self._kebab_menu.add_command(label="Выгрузка в файлы…", command=self.dump_ctrl.open_window)
        self._kebab_menu.add_separator()
        self._kebab_menu.add_command(label="Проверить обновления", command=self.check_update)
        self._kebab_menu.add_command(label="Папка данных", command=self._open_data_dir)
        self._kebab_menu.add_command(label="Показать лог", command=self._open_server_log)
        self._kebab_menu.add_command(label="Открыть cfsmcp2.ini", command=self._open_ini_file)
        self._kebab_menu.add_separator()
        self._kebab_menu.add_command(label="GitHub", command=self._open_github_home)
        self._kebab_menu.add_command(label="О программе", command=self._show_about)
        self._kebab_menu.add_separator()
        self._kebab_menu.add_command(label="Выход", command=self.exit_app)

    def _show_kebab_menu(self, _event: tk.Event | None = None) -> None:
        try:
            x = self.kebab_btn.winfo_rootx()
            y = self.kebab_btn.winfo_rooty() + self.kebab_btn.winfo_height()
            self._kebab_menu.tk_popup(x, y)
        finally:
            try:
                self._kebab_menu.grab_release()
            except tk.TclError:
                pass

    def _notify_server_crash(self, code: int) -> None:
        self._last_crash_code = code
        self._set_crash_status_band(code)
        if self._window_visible():
            self._show_server_crash_dialog(code)
        else:
            self._pending_crash_notice = code
            self._tray_notify(
                f"Сервер завершился (код {code}, порт {self._tray_port()}). "
                "Откройте окно для подробностей."
            )

    def _close_crash_dialog(self) -> None:
        if self._crash_dialog is not None:
            try:
                self._crash_dialog.destroy()
            except tk.TclError:
                pass
            self._crash_dialog = None

    def _show_server_crash_dialog(self, code: int) -> None:
        if self._crash_dialog is not None:
            try:
                if self._crash_dialog.winfo_exists():
                    self._crash_dialog.lift()
                    self._crash_dialog.focus_force()
                    return
            except tk.TclError:
                self._crash_dialog = None

        win = tk.Toplevel(self.root)
        self._crash_dialog = win
        win.title("cfsmcp2")
        win.transient(self.root)
        win.grab_set()
        win.configure(bg=self.theme.SURFACE)
        win.resizable(False, False)

        body = tk.Frame(win, bg=self.theme.SURFACE, padx=self.theme.sp(20), pady=self.theme.sp(16))
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            body,
            text=f"Сервер завершился (код {code}).",
            font=self.theme.font_body("bold"),
            fg=self.theme.FG,
            bg=self.theme.SURFACE,
            anchor="w",
        ).pack(fill=tk.X)
        tk.Label(
            body,
            text="Подробности: data\\server.log и data\\crash.log",
            font=self.theme.font_sm(),
            fg=self.theme.MUTED,
            bg=self.theme.SURFACE,
            anchor="w",
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(self.theme.sp(8), self.theme.sp(16)))

        btn_row = tk.Frame(body, bg=self.theme.SURFACE)
        btn_row.pack(fill=tk.X)
        for col in range(3):
            btn_row.columnconfigure(col, weight=1, uniform="crash")

        log_btn = self.theme.make_button(btn_row, "Открыть лог", self._open_server_log)
        log_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        folder_btn = self.theme.make_button(btn_row, "Открыть папку", self._open_data_dir)
        folder_btn.grid(row=0, column=1, sticky="ew", padx=4)
        ok_btn = self.theme.make_button(btn_row, "OK", self._close_crash_dialog)
        ok_btn.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        win.bind("<Escape>", lambda _e: self._close_crash_dialog())
        win.bind("<Return>", lambda _e: self._close_crash_dialog())

        win.update_idletasks()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        ww = win.winfo_reqwidth()
        wh = win.winfo_reqheight()
        win.geometry(f"+{rx + (rw - ww) // 2}+{ry + (rh - wh) // 2}")

        def _on_close() -> None:
            self._close_crash_dialog()

        win.protocol("WM_DELETE_WINDOW", _on_close)

    def _validate_port_input(self, proposed: str) -> bool:
        return proposed == "" or (proposed.isdigit() and len(proposed) <= 5)

    def _port_field_valid(self) -> bool:
        text = self.port_var.get().strip()
        if not text:
            return False
        try:
            self._port_from_ui()
            return True
        except ValueError:
            return False

    def _on_port_field_changed(self) -> None:
        if not self._ui_ready:
            return
        self._refresh_url_fields()
        valid = self._port_field_valid()
        self.port_error_var.set("" if valid else "Порт 1–65535")
        if valid:
            self.pick_port_btn.pack_forget()
        if self.proc and self.proc.poll() is None:
            return
        self._update_action_buttons()
        self._schedule_tray_tooltip_update()

    def _schedule_tray_tooltip_update(self) -> None:
        if self._tray_tooltip_after_id is not None:
            try:
                self.root.after_cancel(self._tray_tooltip_after_id)
            except tk.TclError:
                pass
        self._tray_tooltip_after_id = self.root.after(300, self._run_tray_tooltip_update)

    def _run_tray_tooltip_update(self) -> None:
        self._tray_tooltip_after_id = None
        self._update_tray_tooltip()

    def _toggle_advanced(self) -> None:
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self.advanced_body.pack(fill=tk.X, pady=(8, 0))
            self.advanced_title_var.set("Дополнительно  ▴")
        else:
            self.advanced_body.pack_forget()
            self.advanced_title_var.set("Дополнительно  ▾")
        self._grow_to_fit()

    def _save_advanced_settings(self) -> None:
        if not self._ui_ready:
            return
        url = self.lm_studio_var.get().strip()
        if url and not is_valid_http_url(url):
            self.lm_studio_error_var.set("URL должен начинаться с http:// или https://")
            return
        self.lm_studio_error_var.set("")
        self.ini.lm_studio_url = url or self.ini.lm_studio_url
        self.ini.minimize_to_tray = bool(self.minimize_tray_var.get())
        self.ini.autostart_server = bool(self.autostart_server_var.get())
        save_portable_ini(self.ini_path, self.ini)

    def _pick_free_port(self) -> None:
        host = self._host_from_ui()
        start = self._current_port()
        free = find_free_port(host, start)
        if free is None:
            self._show_error("cfsmcp2", "Не удалось подобрать свободный порт поблизости.")
            return
        self.port_var.set(str(free))
        self.port_error_var.set("")
        self.pick_port_btn.pack_forget()
        self._save_network_settings()

    def _save_network_settings(self) -> None:
        if not self._ui_ready:
            return
        self._refresh_url_fields()
        if self.proc and self.proc.poll() is None:
            self._schedule_tray_tooltip_update()
            return
        if not self._port_field_valid():
            self.port_error_var.set("Порт 1–65535")
            self._schedule_tray_tooltip_update()
            return
        self.port_error_var.set("")
        try:
            self._read_ini_from_form()
        except ValueError:
            self.port_error_var.set("Порт 1–65535")
        self._schedule_tray_tooltip_update()

    def _on_network_settings_changed(self) -> None:
        if is_network_bind(self._host_from_ui()):
            self._refresh_local_ip_cache()
        self._refresh_url_fields()
        self._save_network_settings()
        self._grow_to_fit()

    def _read_ini_from_form(self) -> PortableIni:
        port = self._port_from_ui()
        lm_url = self.lm_studio_var.get().strip() if hasattr(self, "lm_studio_var") else self.ini.lm_studio_url
        if lm_url and not is_valid_http_url(lm_url):
            raise ValueError("invalid lm studio url")
        self.ini = PortableIni(
            host=self._host_from_ui(),
            port=port,
            lm_studio_url=lm_url or self.ini.lm_studio_url,
            minimize_to_tray=(
                bool(self.minimize_tray_var.get()) if hasattr(self, "minimize_tray_var") else self.ini.minimize_to_tray
            ),
            tray_hint_shown=self.ini.tray_hint_shown,
            first_run_hint_shown=self.ini.first_run_hint_shown,
            autostart_server=(
                bool(self.autostart_server_var.get())
                if hasattr(self, "autostart_server_var")
                else self.ini.autostart_server
            ),
        )
        save_portable_ini(self.ini_path, self.ini)
        self._refresh_url_fields()
        return self.ini

    def _server_process_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _update_action_buttons(self) -> None:
        if not self._ui_ready:
            return
        alive = self._server_process_alive()
        if alive:
            if self._server_ready:
                self.primary_btn.configure(text="Остановить", command=self.stop_server)
                self.theme.set_button_kind(self.primary_btn, "stop")
                self.theme.set_button_enabled(self.primary_btn, True)
                self.theme.set_button_enabled(self.secondary_btn, True)
            else:
                self.primary_btn.configure(text="Отмена", command=self.stop_server)
                self.theme.set_button_kind(self.primary_btn, "footer")
                self.theme.set_button_enabled(self.primary_btn, True)
                self.theme.set_button_enabled(self.secondary_btn, False)
        else:
            self.primary_btn.configure(text="Запустить", command=self.start_server)
            self.theme.set_button_kind(self.primary_btn, "primary")
            self.theme.set_button_enabled(self.primary_btn, self._port_field_valid())
            self.theme.set_button_enabled(self.secondary_btn, False)

    def _set_running(self, running: bool) -> None:
        if not self._ui_ready:
            return
        if running:
            self._server_ready = False
            self._startup_timed_out = False
            self._last_crash_code = None
            self._start_attempt_at = time.monotonic()
            self._set_status_band("loading", "Запуск…")
            self.bind_local_rb.configure(state=tk.DISABLED)
            self.bind_network_rb.configure(state=tk.DISABLED)
            self.port_entry.configure(state=tk.DISABLED)
            if hasattr(self, "lm_studio_entry"):
                self.lm_studio_entry.configure(state=tk.DISABLED)
        else:
            self._server_ready = False
            self._start_attempt_at = None
            self._startup_timed_out = False
            self._proc_started_at = None
            self._schedule_uptime_tick()
            self.pick_port_btn.pack_forget()
            if self._last_crash_code is not None:
                self._set_crash_status_band(self._last_crash_code)
            else:
                self._set_status_band("stopped", "Остановлен")
            self.bind_local_rb.configure(state=tk.NORMAL)
            self.bind_network_rb.configure(state=tk.NORMAL)
            self.port_entry.configure(state=tk.NORMAL)
            if hasattr(self, "lm_studio_entry"):
                self.lm_studio_entry.configure(state=tk.NORMAL)
        self._update_action_buttons()
        self._update_tray_tooltip()

    def open_ui(self) -> None:
        if not self._server_ready:
            return
        webbrowser.open(self._ui_url())

    def _window_visible(self) -> bool:
        try:
            return bool(self.root.winfo_viewable())
        except tk.TclError:
            return False

    def _tray_port(self) -> int:
        try:
            return self._current_port()
        except Exception:
            return int(self.ini.port)

    def _tray_notify_title(self) -> str:
        return f"cfsmcp2 — порт {self._tray_port()}"

    def _tray_notify(self, message: str, title: str | None = None) -> bool:
        if self.tray_icon is None:
            return False
        try:
            self.tray_icon.notify(message, title or self._tray_notify_title())
            return True
        except Exception:
            return False

    def _show_error(self, title: str, message: str) -> None:
        if self._window_visible():
            messagebox.showerror(title, message, parent=self.root)
            return
        full = f"{title}\n{message}"
        if len(full) > 240:
            self._pending_tray_message = ("error", title, message)
            self._tray_notify(f"{title} — откройте окно для подробностей")
        else:
            self._tray_notify(full)

    def _show_info(self, title: str, message: str) -> None:
        if self._window_visible():
            messagebox.showinfo(title, message, parent=self.root)
            return
        full = f"{title}\n{message}"
        if len(full) > 240:
            self._pending_tray_message = ("info", title, message)
            self._tray_notify(f"{title} — откройте окно для подробностей")
        else:
            self._tray_notify(full)

    def _flush_pending_tray_message(self) -> None:
        if self._pending_tray_message is None:
            return
        kind, title, message = self._pending_tray_message
        self._pending_tray_message = None
        if kind == "info":
            messagebox.showinfo(title, message, parent=self.root)
        else:
            messagebox.showerror(title, message, parent=self.root)

    def _ask_yesno(self, title: str, message: str) -> bool:
        if not self._window_visible():
            self._show_window(flush_pending=False)
        result = bool(messagebox.askyesno(title, message, parent=self.root))
        self._flush_pending_tray_message()
        return result

    def _set_update_status(self, text: str) -> None:
        self._set_status_band("loading", text, force=True)

    def _ini_for_health(self) -> PortableIni:
        try:
            return PortableIni(
                host=self._host_from_ui(),
                port=self._port_from_ui(),
                lm_studio_url=self.ini.lm_studio_url,
                minimize_to_tray=self.ini.minimize_to_tray,
                tray_hint_shown=self.ini.tray_hint_shown,
                first_run_hint_shown=self.ini.first_run_hint_shown,
                autostart_server=self.ini.autostart_server,
            )
        except ValueError:
            return self.ini

    def start_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        if not self._port_field_valid():
            self.port_error_var.set("Порт 1–65535")
            self._show_error("cfsmcp2", "Некорректный порт. Укажите значение от 1 до 65535.")
            return
        try:
            ini = self._read_ini_from_form()
        except ValueError as exc:
            self._show_error("cfsmcp2", f"Некорректный порт:\n{exc}")
            return
        try:
            cmd, cwd = _server_executable(self.root_dir)
        except FileNotFoundError as exc:
            self._show_error("cfsmcp2", str(exc))
            return
        if not is_port_available(ini.host, ini.port):
            self.port_error_var.set(f"Порт {ini.port} занят другим процессом")
            self.pick_port_btn.pack(side=tk.LEFT, padx=(self.theme.sp(6), 0))
            self._grow_to_fit()
            return
        self.pick_port_btn.pack_forget()
        env = _build_server_env(ini)
        flags = CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                creationflags=flags,
            )
        except OSError as exc:
            self._show_error("cfsmcp2", f"Не удалось запустить сервер:\n{exc}")
            return
        log.info("server started pid=%s cmd=%s", self.proc.pid, cmd)
        self._proc_started_at = time.monotonic()
        self._set_running(True)

    def stop_server(self) -> None:
        if not self.proc:
            self._set_running(False)
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        self._set_running(False)

    def _poll_server(self) -> None:
        if self._closing:
            return
        if self.proc and self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            self._set_running(False)
            if not self._closing:
                self._notify_server_crash(code)
        elif self.proc and not self._health_inflight:
            self._health_inflight = True
            ini = self._ini_for_health()

            def work() -> None:
                ok = False
                try:
                    ok = _health_ok(ini)
                except Exception:
                    log.debug("health check thread failed", exc_info=True)
                try:
                    self.root.after(0, lambda result=ok: self._on_health_result(result))
                except tk.TclError:
                    self._health_inflight = False

            threading.Thread(target=work, daemon=True).start()
        if self.proc is None:
            delay = 3000
        elif self._server_ready:
            delay = self._health_poll_ms_ready
        else:
            delay = self._health_poll_ms
        self.root.after(delay, self._poll_server)
        if self.dump_ctrl.should_poll():
            self.dump_ctrl.poll_tick()

    def _refresh_dump_tray_menu(self) -> None:
        if self.tray_icon is None:
            return
        try:
            import pystray

            self.tray_icon.menu = self._build_tray_menu()
            self.tray_icon.update_menu()
        except Exception:
            log.debug("tray menu refresh failed", exc_info=True)

    def _build_tray_menu(self):
        import pystray

        def show_window(icon, _item) -> None:
            self.root.after(0, self._show_window)

        def tray_start(icon, _item) -> None:
            self.root.after(0, self.start_server)

        def tray_stop(icon, _item) -> None:
            self.root.after(0, self.stop_server)

        def tray_open(icon, _item) -> None:
            self.root.after(0, self.open_ui)

        def tray_dump_window(icon, _item) -> None:
            self.root.after(0, self.dump_ctrl.open_window)

        def tray_data(icon, _item) -> None:
            self.root.after(0, self._open_data_dir)

        def tray_log(icon, _item) -> None:
            self.root.after(0, self._open_server_log)

        def tray_exit(icon, _item) -> None:
            self.root.after(0, self.exit_app)

        items: list = [
            pystray.MenuItem("Показать окно", show_window, default=True),
            pystray.MenuItem("Открыть UI", tray_open),
            pystray.Menu.SEPARATOR,
        ]
        quick = self.dump_ctrl.tray_submenu_items()
        if quick:
            dump_items = [
                pystray.MenuItem(label, lambda _i, _it, fn=fn: self.root.after(0, fn))
                for label, fn in quick
            ]
            items.append(pystray.MenuItem("Выгрузить", pystray.Menu(*dump_items)))
        items.extend(
            [
                pystray.MenuItem("Выгрузка в файлы…", tray_dump_window),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Старт", tray_start),
                pystray.MenuItem("Стоп", tray_stop),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Папка данных", tray_data),
                pystray.MenuItem("Показать лог", tray_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", tray_exit),
            ]
        )
        return pystray.Menu(*items)

    def _on_health_result(self, ok: bool) -> None:
        self._health_inflight = False
        if self._closing or not self.proc or self.proc.poll() is not None:
            return
        if ok:
            self._startup_timed_out = False
            if not self._server_ready:
                self._server_ready = True
                self._dismiss_first_run_hint()
                self._schedule_uptime_tick()
                self._update_action_buttons()
            self._set_status_band("running", "Запущен")
            self.dump_ctrl.on_server_ready()
            if self.dump_ctrl.should_poll():
                self.dump_ctrl.poll_tick()
        else:
            if self._server_ready:
                self._server_ready = False
                self._schedule_uptime_tick()
                self._update_action_buttons()
            if (
                self._start_attempt_at is not None
                and time.monotonic() - self._start_attempt_at >= STARTUP_TIMEOUT_SEC
            ):
                self._startup_timed_out = True
                self._set_status_band("unresponsive", "Не отвечает")
            else:
                self._set_status_band("loading", "Запуск…")

    def _bind_version_update(self) -> None:
        self._version_label = f"v{APP_VERSION}"
        self.version_badge.configure(cursor="hand2")
        Tooltip(
            self.version_badge,
            "Проверить обновления",
            bg=self.theme.INPUT_BG,
            fg=self.theme.FG,
            border=self.theme.CARD_BORDER,
        )
        self.version_badge.bind("<Button-1>", lambda _e: self.check_update())
        self.version_badge.bind(
            "<Enter>",
            lambda _e: self.version_badge.configure(
                fg=self.theme.ACCENT_FG if not self._update_busy else self.theme.MUTED,
                bg=self.theme.ACCENT if not self._update_busy else self.theme.INPUT_BG,
                highlightbackground=self.theme.ACCENT if not self._update_busy else self.theme.CARD_BORDER,
            ),
            add="+",
        )
        self.version_badge.bind(
            "<Leave>",
            lambda _e: self._restore_version_badge_style(),
            add="+",
        )

    def _restore_version_badge_style(self) -> None:
        if self._update_busy:
            self.version_badge.configure(
                text="…",
                fg=self.theme.MUTED,
                bg=self.theme.INPUT_BG,
                highlightbackground=self.theme.CARD_BORDER,
                cursor="watch",
            )
            return
        self.version_badge.configure(
            text=self._version_label,
            fg=self.theme.ACCENT,
            bg=self.theme.SURFACE,
            highlightbackground=self.theme.CARD_BORDER,
            cursor="hand2",
        )

    def _set_version_busy(self, busy: bool) -> None:
        self._update_busy = busy
        self._restore_version_badge_style()

    def _restore_status_after_update(self) -> None:
        if self._update_busy:
            return
        self._restore_status_band()

    def check_update(self) -> None:
        if not self._ui_ready or self._update_busy:
            return
        from app.portable.update import check_github_update

        self._set_update_status("Проверка обновлений…")
        self._set_version_busy(True)
        self.root.update_idletasks()

        def work() -> None:
            info = check_github_update()
            self.root.after(0, lambda: self._show_update_result(info))

        threading.Thread(target=work, daemon=True).start()

    def _close_update_window(self) -> None:
        if self._update_window is not None:
            try:
                self._update_window.destroy()
            except tk.TclError:
                pass
        self._update_window = None
        self._update_progress_var = None
        self._update_status_var = None

    def _center_toplevel(self, win: tk.Toplevel) -> None:
        win.update_idletasks()
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        ww = win.winfo_reqwidth()
        wh = win.winfo_reqheight()
        win.geometry(f"+{rx + (rw - ww) // 2}+{ry + (rh - wh) // 2}")

    def _ensure_update_window(self) -> tk.Toplevel:
        if self._update_window is not None:
            try:
                if self._update_window.winfo_exists():
                    for child in self._update_window.winfo_children():
                        child.destroy()
                    return self._update_window
            except tk.TclError:
                pass
        win = tk.Toplevel(self.root)
        win.title("Обновление cfsmcp2")
        win.transient(self.root)
        win.configure(bg=self.theme.SURFACE)
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._close_update_window)
        self._update_window = win
        return win

    def _show_update_offer(self, info) -> None:
        win = self._ensure_update_window()
        body = tk.Frame(win, bg=self.theme.SURFACE, padx=self.theme.sp(20), pady=self.theme.sp(16))
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            body,
            text=f"Доступна версия {info.latest}\nСейчас установлена {info.current}",
            font=self.theme.font_body(),
            fg=self.theme.FG,
            bg=self.theme.SURFACE,
            justify=tk.LEFT,
        ).pack(anchor="w")
        notes = tk.Label(
            body,
            text="Примечания к выпуску ↗",
            font=self.theme.font_sm(),
            fg=self.theme.INFO,
            bg=self.theme.SURFACE,
            cursor="hand2",
        )
        notes.pack(anchor="w", pady=(self.theme.sp(8), self.theme.sp(16)))
        notes.bind("<Button-1>", lambda _e: webbrowser.open(info.release_url))
        btn_row = tk.Frame(body, bg=self.theme.SURFACE)
        btn_row.pack(fill=tk.X)
        btn_row.columnconfigure(0, weight=1, uniform="upd")
        btn_row.columnconfigure(1, weight=1, uniform="upd")
        if info.download_url and info.download_name:
            self.theme.make_button(btn_row, "Установить", lambda: self._install_update(info)).grid(
                row=0, column=0, sticky="ew", padx=(0, 4)
            )
        else:
            self.theme.make_button(
                btn_row,
                "Открыть релиз",
                lambda: webbrowser.open(info.release_url),
            ).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.theme.make_button(btn_row, "Позже", self._close_update_window).grid(
            row=0, column=1, sticky="ew", padx=(4, 0)
        )
        self._center_toplevel(win)

    def _show_update_progress(self, info) -> None:
        win = self._ensure_update_window()
        body = tk.Frame(win, bg=self.theme.SURFACE, padx=self.theme.sp(20), pady=self.theme.sp(16))
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            body,
            text=f"Обновление до версии {info.latest}",
            font=self.theme.font_body("bold"),
            fg=self.theme.FG,
            bg=self.theme.SURFACE,
        ).pack(anchor="w")
        self._update_status_var = tk.StringVar(value="Подготовка…")
        tk.Label(
            body,
            textvariable=self._update_status_var,
            font=self.theme.font_sm(),
            fg=self.theme.MUTED,
            bg=self.theme.SURFACE,
        ).pack(anchor="w", pady=(self.theme.sp(8), self.theme.sp(4)))
        self._update_progress_var = tk.DoubleVar(value=0.0)
        ttk.Progressbar(body, variable=self._update_progress_var, maximum=100).pack(
            fill=tk.X, pady=(0, self.theme.sp(12))
        )
        self._center_toplevel(win)

    def _update_download_progress(self, downloaded: int, total: int | None) -> None:
        if self._update_status_var is None or self._update_progress_var is None:
            return
        if total and total > 0:
            self._update_progress_var.set(100.0 * downloaded / total)
            self._update_status_var.set(
                f"Загрузка: {downloaded / 1_048_576:.1f} / {total / 1_048_576:.1f} МБ"
            )
        else:
            self._update_progress_var.set(0.0)
            self._update_status_var.set(f"Загрузка: {downloaded / 1_048_576:.1f} МБ")

    def _show_update_ready(self, info, staging_root: Path) -> None:
        win = self._ensure_update_window()
        body = tk.Frame(win, bg=self.theme.SURFACE, padx=self.theme.sp(20), pady=self.theme.sp(16))
        body.pack(fill=tk.BOTH, expand=True)
        tk.Label(
            body,
            text=f"Версия {info.latest} готова к установке.",
            font=self.theme.font_body(),
            fg=self.theme.FG,
            bg=self.theme.SURFACE,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, self.theme.sp(16)))
        btn_row = tk.Frame(body, bg=self.theme.SURFACE)
        btn_row.pack(fill=tk.X)
        btn_row.columnconfigure(0, weight=1, uniform="upd")
        btn_row.columnconfigure(1, weight=1, uniform="upd")

        def restart() -> None:
            self._close_update_window()
            self._restart_for_update(staging_root)

        def later() -> None:
            self._close_update_window()
            hint = ""
            if os.name == "nt":
                hint = (
                    "\n\nЕсли авто-перезапуск не сработает: закройте cfsmcp2 "
                    "и запустите _updates\\update-portable.cmd."
                )
            self._show_info(
                "Обновление",
                "Перезапустите cfsmcp2 вручную — обновление будет завершено при запуске."
                + hint,
            )

        self.theme.make_button(btn_row, "Перезапустить", restart).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        self.theme.make_button(btn_row, "Позже", later).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self._center_toplevel(win)

    def _show_update_result(self, info) -> None:
        self._set_version_busy(False)
        self._restore_status_after_update()

        if info.error:
            self._show_error(
                "Обновление",
                f"Не удалось проверить обновления:\n{info.error}\n\n{info.release_url}",
            )
            return

        if not info.newer:
            self._show_info(
                "Обновление",
                f"Установлена актуальная версия ({info.current}).",
            )
            return

        self._show_update_offer(info)

    def _install_update(self, info) -> None:
        from app.portable.update import (
            download_update_zip,
            extract_portable_zip,
            pending_update_dir,
        )

        if not info.download_url or not info.download_name:
            webbrowser.open(info.release_url)
            return

        self._show_update_progress(info)
        self._set_version_busy(True)
        zip_path = self.root_dir / "_updates" / info.download_name
        staging_dir = pending_update_dir(self.root_dir, info.latest)

        def work() -> None:
            try:
                def progress(downloaded: int, total: int | None) -> None:
                    self.root.after(0, lambda: self._update_download_progress(downloaded, total))

                download_update_zip(info.download_url, zip_path, progress_callback=progress)

                def _mark_installing() -> None:
                    if self._update_status_var is not None:
                        self._update_status_var.set("Установка…")

                self.root.after(0, _mark_installing)
                staging_root = extract_portable_zip(zip_path, staging_dir)
                if os.name == "nt":
                    from app.portable.update import prepare_update_helper

                    prepare_update_helper(self.root_dir, staging_root)
                self.root.after(0, lambda: self._install_update_done(info, staging_root, None))
            except Exception as exc:
                self.root.after(0, lambda: self._install_update_done(info, None, exc))

        threading.Thread(target=work, daemon=True).start()

    def _install_update_done(self, info, staging_root: Path | None, error: Exception | None) -> None:
        self._set_version_busy(False)
        self._restore_status_after_update()
        if error is not None:
            self._close_update_window()
            self._show_error("Обновление", f"Ошибка установки:\n{error}")
            return
        assert staging_root is not None
        self._show_update_ready(info, staging_root)

    def _restart_for_update(self, staging_root: Path) -> None:
        if self.proc and self.proc.poll() is None:
            if not self._ask_yesno("Обновление", "Остановить сервер и перезапустить cfsmcp2?"):
                return
        self._closing = True
        self.stop_server()
        self._stop_tray()
        if self.instance_lock is not None:
            self.instance_lock.release()
            self.instance_lock = None
        from app.portable.update import (
            find_staging_updater_exe,
            prepare_update_helper,
            spawn_staging_apply_update,
            spawn_windows_update_apply,
        )

        try:
            if os.name == "nt":
                prepare_update_helper(self.root_dir, staging_root)
                install_exe = Path(sys.executable)
                if find_staging_updater_exe(staging_root, install_exe) is not None:
                    spawn_staging_apply_update(staging_root, self.root_dir, os.getpid())
                else:
                    spawn_windows_update_apply(staging_root, self.root_dir, os.getpid())
            else:
                exe = Path(sys.executable)
                cmd = [
                    str(exe),
                    "--apply-update",
                    f"--source={staging_root}",
                    f"--target={self.root_dir}",
                    f"--wait-pid={os.getpid()}",
                ]
                subprocess.Popen(cmd, cwd=str(self.root_dir))
        except OSError as exc:
            self._closing = False
            self._show_error("Обновление", f"Не удалось перезапустить:\n{exc}")
            return
        self._dismiss_floating_ui()
        self.root.destroy()
        sys.exit(0)

    def _open_github_home(self) -> None:
        from app.portable.update import GITHUB_HOME

        webbrowser.open(GITHUB_HOME)

    def _maybe_show_tray_hint(self) -> None:
        if self.ini.tray_hint_shown or self._closing:
            return
        port = self._tray_port()
        if self._tray_notify(f"cfsmcp2 (порт {port}) продолжает работать в области уведомлений."):
            self.ini.tray_hint_shown = True
            save_portable_ini(self.ini_path, self.ini)

    def _dismiss_floating_ui(self) -> None:
        """Close tooltips and auxiliary windows before hide/exit (Windows ghost fix)."""
        Tooltip.hide_all()
        self._close_crash_dialog()
        self._close_update_window()
        self.dump_ctrl._close_window()
        if self._flash_after_id is not None:
            try:
                self.root.after_cancel(self._flash_after_id)
            except tk.TclError:
                pass
            self._flash_after_id = None
            self._status_flash_saved = None
        if self._status_progress_running:
            try:
                self.status_progress.stop()
                self.status_progress.pack_forget()
            except tk.TclError:
                pass
            self._status_progress_running = False
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass

    def _hide_to_tray(self) -> None:
        if self._closing:
            return
        self._dismiss_floating_ui()
        self.root.withdraw()
        self._ensure_tray()
        if not self.ini.tray_hint_shown:
            self.root.after(400, self._maybe_show_tray_hint)

    def _on_root_unmap(self, event: tk.Event) -> None:
        if self._closing or event.widget != self.root:
            return
        if str(self.root.state()) == "iconic":
            self.root.after(0, self._hide_to_tray)

    def _on_close_window(self) -> None:
        if not self._ui_ready:
            self._closing = True
            self._dismiss_floating_ui()
            if self.instance_lock is not None:
                self.instance_lock.release()
                self.instance_lock = None
            self.root.destroy()
            return
        if self.ini.minimize_to_tray:
            self._hide_to_tray()
        else:
            self.exit_app()

    def exit_app(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not self._ask_yesno("cfsmcp2", "Остановить сервер и выйти?"):
                return
        self._closing = True
        self.stop_server()
        self._stop_tray()
        self._dismiss_floating_ui()
        if self.instance_lock is not None:
            self.instance_lock.release()
            self.instance_lock = None
        self.root.destroy()

    def _tray_image(self):
        from app.portable.tray_icon import load_tray_image

        return load_tray_image(
            _resource_path("cfs-mark.ico"),
            state=self._tray_icon_state(),
        )

    def _tray_instance_label(self) -> str:
        name = self.instance_name
        if len(name) > 40:
            return name[:37] + "..."
        return name

    def _tray_title(self) -> str:
        running = self.proc is not None and self.proc.poll() is None
        state = "запущен" if running else "остановлен"
        return f"cfsmcp2 — порт {self._tray_port()}, {state}, {self._tray_instance_label()}"

    def _update_tray_tooltip(self) -> None:
        if self.tray_icon is None:
            return
        self.tray_icon.title = self._tray_title()

    def _ensure_tray(self) -> None:
        if self.tray_icon is not None:
            return
        try:
            import pystray
        except ImportError:
            return

        self._tray_icon_current = None
        self.tray_icon = pystray.Icon(
            "cfsmcp2",
            self._tray_image(),
            self._tray_title(),
            self._build_tray_menu(),
        )
        self._tray_icon_current = self._tray_icon_state()

        def run_tray() -> None:
            assert self.tray_icon is not None
            self.tray_icon.run()

        self.tray_thread = threading.Thread(target=run_tray, daemon=True)
        self.tray_thread.start()
        self._update_tray_tooltip()

    def _show_window(self, *, flush_pending: bool = True) -> None:
        try:
            self.root.state("normal")
        except tk.TclError:
            pass
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        if self._pending_crash_notice is not None:
            code = self._pending_crash_notice
            self._pending_crash_notice = None
            self._last_crash_code = code
            self._set_crash_status_band(code)
            self._show_server_crash_dialog(code)
        elif flush_pending:
            self._flush_pending_tray_message()

    def _stop_tray(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None
        self._tray_icon_current = None

    def run(self) -> None:
        self.root.mainloop()


def _write_crash(text: str) -> None:
    try:
        from datetime import datetime

        path = portable_exe_dir() / "data" / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- {stamp} ---\n{text}")
    except OSError:
        pass


def _show_fatal_error(text: str) -> None:
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("cfsmcp2", text, parent=root)
        root.destroy()
    except Exception:
        pass


def _null_stream():
    """Windowed exe has no console; avoid writing raw stdout to server.log."""
    try:
        return open(os.devnull, "w", encoding="utf-8")
    except OSError:
        return None


def _ensure_stdio() -> None:
    """Windowed PyInstaller exe has no console; logging goes via app.core.logging_setup."""
    stream = _null_stream()
    if sys.stdout is None and stream is not None:
        sys.stdout = stream
    if sys.stderr is None and stream is not None:
        sys.stderr = stream


def _parse_apply_update_args() -> tuple[Path, Path, int]:
    source: Path | None = None
    target: Path | None = None
    wait_pid: int | None = None
    for arg in sys.argv[1:]:
        if arg.startswith("--source="):
            source = Path(arg.split("=", 1)[1])
        elif arg.startswith("--target="):
            target = Path(arg.split("=", 1)[1])
        elif arg.startswith("--wait-pid="):
            wait_pid = int(arg.split("=", 1)[1])
    if source is None or target is None or wait_pid is None:
        raise SystemExit("apply-update: missing --source, --target or --wait-pid")
    return source, target, wait_pid


def _run_apply_update_mode() -> None:
    import traceback

    from app.portable.update import apply_portable_update, cleanup_pending_staging, spawn_windows_update_apply, wait_for_process

    source, target, wait_pid = _parse_apply_update_args()
    if os.name == "nt":
        try:
            spawn_windows_update_apply(source, target, os.getpid())
        except Exception:
            log.exception("apply-update failed")
            _write_crash(traceback.format_exc())
            raise
        sys.exit(0)

    try:
        wait_for_process(wait_pid)
        apply_portable_update(source, target)
        cleanup_pending_staging(source, target)
    except Exception:
        log.exception("apply-update failed")
        _write_crash(traceback.format_exc())
        raise

    exe = target / "cfsmcp2.exe"
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen([str(exe)], cwd=str(target), creationflags=flags)


def _run_server_mode() -> None:
    import traceback

    from app.main import run

    _ensure_stdio()
    try:
        run()
    except Exception:
        msg = traceback.format_exc()
        log.exception("server failed")
        _write_crash(msg)
        raise


def main() -> None:
    import traceback

    if "--apply-update" in sys.argv:
        _run_apply_update_mode()
        return

    if "--server" in sys.argv:
        _run_server_mode()
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _enable_dpi_awareness()
    root_dir = portable_exe_dir()
    data_dir = default_data_dir()
    instance_lock = LauncherInstanceLock(root_dir)
    if not instance_lock.acquire():
        _show_fatal_error(
            "cfsmcp2 уже запущен из этого каталога.\n\n"
            f"Каталог: {root_dir.name}\n"
            f"Данные: {data_dir}\n\n"
            "Если окно скрыто — откройте значок в трее или выберите «Выход»."
        )
        return
    try:
        from app.portable.update import apply_pending_update_if_any

        if apply_pending_update_if_any(root_dir, wait_pid=os.getpid()):
            instance_lock.release()
            sys.exit(0)
        PortableLauncher(instance_lock=instance_lock).run()
    except Exception:
        instance_lock.release()
        msg = traceback.format_exc()
        log.exception("launcher failed")
        _write_crash(msg)
        _show_fatal_error(msg)
        raise


if __name__ == "__main__":
    main()
