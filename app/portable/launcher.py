"""Windows portable GUI launcher: start/stop server, tray, GitHub update check."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from app.core.paths import default_data_dir, is_frozen, portable_exe_dir
from app.portable.instance_lock import LauncherInstanceLock
from app.portable.ui_theme import LauncherTheme
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


def _health_ok(ini: PortableIni, timeout: float = 1.5) -> bool:
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
        self._closing = False
        self._ui_ready = False
        self._pending_update = None
        self._pending_staging: Path | None = None
        self._update_busy = False
        self._server_ready = False
        self.theme = LauncherTheme

        self.root = tk.Tk()
        self.root.title("cfsmcp2")
        self.root.resizable(False, False)
        self.root.configure(bg=self.theme.BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_window)
        self.root.bind("<Unmap>", self._on_root_unmap)
        self.style = self.theme.apply_ttk(self.root)

        self.body = ttk.Frame(self.root, style="Root.TFrame", padding=(0, 0, 0, 12))
        self.body.pack(fill=tk.BOTH, expand=True)
        self._build_header_shell()
        self.root.update_idletasks()
        self.root.update()
        self.root.after(0, self._finish_init)

    def _build_header_shell(self) -> None:
        accent = tk.Frame(self.body, bg=self.theme.ACCENT, height=3)
        accent.pack(fill=tk.X)
        header = ttk.Frame(self.body, style="Header.TFrame", padding=(16, 10))
        header.pack(fill=tk.X)

        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        title_row = tk.Frame(left, bg=self.theme.SURFACE)
        title_row.pack(anchor="w")
        self.brand_label = tk.Label(
            title_row,
            text="cfsmcp2",
            font=self.theme.font_title(),
            fg=self.theme.FG,
            bg=self.theme.SURFACE,
            cursor="hand2",
        )
        self.brand_label.pack(side=tk.LEFT)
        self.brand_label.bind("<Button-1>", lambda _e: self._open_github_home())
        self.brand_label.bind("<Enter>", lambda _e: self.brand_label.configure(fg=self.theme.INFO))
        self.brand_label.bind("<Leave>", lambda _e: self.brand_label.configure(fg=self.theme.FG))
        ttk.Label(left, text="Portable launcher", style="HeaderMuted.TLabel").pack(anchor="w", pady=(2, 0))

        right = ttk.Frame(header, style="Header.TFrame")
        right.pack(side=tk.RIGHT)
        self.version_badge = tk.Label(
            right,
            text=f"v{APP_VERSION}",
            font=("Segoe UI", 9, "bold"),
            fg=self.theme.ACCENT,
            bg=self.theme.SURFACE,
            padx=8,
            pady=3,
            highlightthickness=1,
            highlightbackground=self.theme.CARD_BORDER,
        )
        self.version_badge.pack(anchor="e")
        status_row = tk.Frame(right, bg=self.theme.SURFACE)
        status_row.pack(anchor="e", pady=(8, 0))
        self.status_dot = tk.Canvas(status_row, width=12, height=12, bg=self.theme.SURFACE, highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(0, 6))
        self.status_text_var = tk.StringVar(value="Загрузка…")
        tk.Label(
            status_row,
            textvariable=self.status_text_var,
            font=("Segoe UI", 10, "bold"),
            fg=self.theme.FG,
            bg=self.theme.SURFACE,
        ).pack(side=tk.LEFT)
        self._set_status_indicator("loading")

        self.content = ttk.Frame(self.body, style="Root.TFrame")
        self.content.pack(fill=tk.BOTH, expand=True)

    def _set_status_indicator(self, state: str) -> None:
        colors = {
            "loading": self.theme.WARNING,
            "stopped": self.theme.MUTED,
            "running": self.theme.ACCENT,
            "info": self.theme.INFO,
        }
        color = colors.get(state, self.theme.MUTED)
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 10, 10, fill=color, outline=color)

    def _info_row(self, parent: tk.Frame, label: str, value: str) -> None:
        row = tk.Frame(parent, bg=self.theme.CARD)
        row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(row, text=label, font=self.theme.font_sm(), fg=self.theme.MUTED, bg=self.theme.CARD, width=10, anchor="w").pack(
            side=tk.LEFT
        )
        tk.Label(
            row,
            text=value,
            font=self.theme.font_mono(),
            fg=self.theme.FG,
            bg=self.theme.CARD,
            anchor="w",
            wraplength=270,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _finish_init(self) -> None:
        self.ini = load_portable_ini(self.ini_path)
        self._set_window_icon()

        _, info_card = self.theme.card(self.content)
        self.theme.section_title(info_card, "Экземпляр").pack(anchor="w", pady=(0, 8))
        self._info_row(info_card, "Каталог", self.instance_name)
        self._info_row(info_card, "Данные", str(self.data_dir))

        _, access_card = self.theme.card(self.content)
        self.theme.section_title(access_card, "Доступ").pack(anchor="w", pady=(0, 8))

        self.bind_mode_var = tk.StringVar(
            value="network" if is_network_bind(self.ini.host) else "local"
        )
        self.bind_local_rb = ttk.Radiobutton(
            access_card,
            text="Только этот компьютер (127.0.0.1)",
            value="local",
            variable=self.bind_mode_var,
            command=self._on_network_settings_changed,
            state=tk.DISABLED,
        )
        self.bind_local_rb.pack(anchor="w", pady=2)
        self.bind_network_rb = ttk.Radiobutton(
            access_card,
            text="По сети (0.0.0.0 — все интерфейсы)",
            value="network",
            variable=self.bind_mode_var,
            command=self._on_network_settings_changed,
            state=tk.DISABLED,
        )
        self.bind_network_rb.pack(anchor="w", pady=2)

        port_row = tk.Frame(access_card, bg=self.theme.CARD)
        port_row.pack(fill=tk.X, pady=(10, 4))
        tk.Label(port_row, text="Порт", font=self.theme.font_body(), fg=self.theme.MUTED, bg=self.theme.CARD).pack(side=tk.LEFT)
        self.port_var = tk.StringVar(value=str(self.ini.port))
        self.port_entry = ttk.Entry(port_row, textvariable=self.port_var, width=10, state=tk.DISABLED)
        self.port_entry.pack(side=tk.LEFT, padx=(10, 0))
        self.port_var.trace_add("write", lambda *_: self._on_network_settings_changed())

        tk.Frame(access_card, bg=self.theme.CARD_BORDER, height=1).pack(fill=tk.X, pady=(12, 10))

        self.ui_url_var = tk.StringVar(value=self._ui_url())
        self.mcp_url_var = tk.StringVar(value=self._mcp_url())
        self._url_active_style = {
            "font": ("Consolas", 9, "underline"),
            "fg": self.theme.INFO,
            "bg": self.theme.CARD,
            "cursor": "hand2",
            "anchor": "w",
        }
        self._url_idle_style = {
            "font": self.theme.font_mono(),
            "fg": self.theme.MUTED,
            "bg": self.theme.CARD,
            "cursor": "arrow",
            "anchor": "w",
        }

        url_grid = tk.Frame(access_card, bg=self.theme.CARD)
        url_grid.pack(fill=tk.X)
        url_grid.columnconfigure(1, weight=1)

        tk.Label(url_grid, text="UI", font=self.theme.font_sm(), fg=self.theme.MUTED, bg=self.theme.CARD, width=5, anchor="w").grid(
            row=0, column=0, sticky="w", pady=3
        )
        self.ui_url_label = tk.Label(url_grid, textvariable=self.ui_url_var, **self._url_idle_style)
        self.ui_url_label.grid(row=0, column=1, sticky="ew", pady=3)

        tk.Label(url_grid, text="MCP", font=self.theme.font_sm(), fg=self.theme.MUTED, bg=self.theme.CARD, width=5, anchor="w").grid(
            row=1, column=0, sticky="w", pady=3
        )
        self.mcp_url_label = tk.Label(url_grid, textvariable=self.mcp_url_var, **self._url_idle_style)
        self.mcp_url_label.grid(row=1, column=1, sticky="ew", pady=3)

        self.mcp_hint_var = tk.StringVar(value=self._mcp_hint())
        tk.Label(
            url_grid,
            textvariable=self.mcp_hint_var,
            font=self.theme.font_sm(),
            fg=self.theme.MUTED,
            bg=self.theme.CARD,
            wraplength=270,
            justify=tk.LEFT,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        footer = tk.Frame(self.content, bg=self.theme.BG, padx=self.theme.PAD_X)
        footer.pack(fill=tk.X, pady=(8, 0))
        tk.Frame(footer, bg=self.theme.CARD_BORDER, height=1).pack(fill=tk.X, pady=(0, 10))

        btn_row = tk.Frame(footer, bg=self.theme.BG)
        btn_row.pack(fill=tk.X)
        for col in range(3):
            btn_row.columnconfigure(col, weight=1, uniform="actions")

        self.start_btn = self.theme.make_button(btn_row, "Старт", self.start_server, kind="primary")
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.theme.set_button_enabled(self.start_btn, False)

        self.stop_btn = self.theme.make_button(btn_row, "Стоп", self.stop_server, kind="danger")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=4)
        self.theme.set_button_enabled(self.stop_btn, False)

        self.open_ui_btn = self.theme.make_button(btn_row, "Открыть UI", self.open_ui)
        self.open_ui_btn.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        self.theme.set_button_enabled(self.open_ui_btn, False)

        self._bind_version_update()

        self._ui_ready = True
        self._set_running(False)
        self._compact_window()
        self._ensure_tray()
        self.root.after(1000, self._poll_server)

    def _compact_window(self) -> None:
        self.root.update_idletasks()
        w = self.theme.WIN_WIDTH
        h = max(self.root.winfo_reqheight(), 420)
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(w, h)
        self.root.maxsize(w, 900)

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
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            port = DEFAULT_PORT
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

    def _mcp_hint(self) -> str:
        if is_network_bind(self._host_from_ui()):
            return "По сети: замените 127.0.0.1 на IP этого ПК в MCP URL клиента."
        return ""

    def _refresh_url_fields(self) -> None:
        self.ui_url_var.set(self._ui_url())
        self.mcp_url_var.set(self._mcp_url())
        self.mcp_hint_var.set(self._mcp_hint())

    def _copy_text(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()

    def _copy_url_clicked(self, text: str) -> None:
        if not self._ui_ready:
            return
        self._copy_text(text)
        prev = self.status_text_var.get()
        prev_state = "running" if self.proc and self.proc.poll() is None else "stopped"
        self.status_text_var.set("URL скопирован")
        self._set_status_indicator("info")
        self.root.after(
            1500,
            lambda: (self.status_text_var.set(prev), self._set_status_indicator(prev_state)),
        )

    def _bind_url_copy(self) -> None:
        self.ui_url_label.configure(**self._url_active_style)
        self.mcp_url_label.configure(**self._url_active_style)
        self.ui_url_label.bind("<Button-1>", lambda _e: self._copy_url_clicked(self._ui_url()))
        self.mcp_url_label.bind("<Button-1>", lambda _e: self._copy_url_clicked(self._mcp_url()))

    def _unbind_url_copy(self) -> None:
        self.ui_url_label.unbind("<Button-1>")
        self.mcp_url_label.unbind("<Button-1>")
        self.ui_url_label.configure(**self._url_idle_style)
        self.mcp_url_label.configure(**self._url_idle_style)

    def _on_network_settings_changed(self) -> None:
        if not self._ui_ready:
            return
        self._refresh_url_fields()
        if self.proc and self.proc.poll() is None:
            return
        try:
            self._read_ini_from_form()
        except ValueError:
            pass

    def _read_ini_from_form(self) -> PortableIni:
        port = self._port_from_ui()
        self.ini = PortableIni(
            host=self._host_from_ui(),
            port=port,
            lm_studio_url=self.ini.lm_studio_url,
            minimize_to_tray=self.ini.minimize_to_tray,
        )
        save_portable_ini(self.ini_path, self.ini)
        self._refresh_url_fields()
        return self.ini

    def _server_process_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _update_open_ui_button(self) -> None:
        if not self._ui_ready:
            return
        self.theme.set_button_enabled(
            self.open_ui_btn,
            self._server_process_alive() and self._server_ready,
        )

    def _set_running(self, running: bool) -> None:
        if not self._ui_ready:
            return
        if running:
            self._server_ready = False
            self.status_text_var.set("Запуск…")
            self._set_status_indicator("loading")
            self.theme.set_button_enabled(self.start_btn, False)
            self.theme.set_button_enabled(self.stop_btn, True)
            self.bind_local_rb.configure(state=tk.DISABLED)
            self.bind_network_rb.configure(state=tk.DISABLED)
            self.port_entry.configure(state=tk.DISABLED)
        else:
            self._server_ready = False
            self.status_text_var.set("Остановлен")
            self._set_status_indicator("stopped")
            self.theme.set_button_enabled(self.start_btn, True)
            self.theme.set_button_enabled(self.stop_btn, False)
            self.bind_local_rb.configure(state=tk.NORMAL)
            self.bind_network_rb.configure(state=tk.NORMAL)
            self.port_entry.configure(state=tk.NORMAL)
        self._update_open_ui_button()
        self._bind_url_copy()
        self._update_tray_tooltip()

    def open_ui(self) -> None:
        if not self._server_ready:
            return
        webbrowser.open(self._ui_url())

    def start_server(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        try:
            ini = self._read_ini_from_form()
        except ValueError as exc:
            messagebox.showerror("cfsmcp2", f"Некорректный порт:\n{exc}")
            return
        try:
            cmd, cwd = _server_executable(self.root_dir)
        except FileNotFoundError as exc:
            messagebox.showerror("cfsmcp2", str(exc))
            return
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
            messagebox.showerror("cfsmcp2", f"Не удалось запустить сервер:\n{exc}")
            return
        log.info("server started pid=%s cmd=%s", self.proc.pid, cmd)
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
        if self.proc and self.proc.poll() is not None:
            code = self.proc.returncode
            self.proc = None
            self._set_running(False)
            if not self._closing:
                messagebox.showwarning(
                    "cfsmcp2",
                    f"Сервер завершился (код {code}).\n\n"
                    "Подробности: data\\server.log и data\\crash.log",
                )
        elif self.proc:
            try:
                ini = PortableIni(
                    host=self._host_from_ui(),
                    port=self._port_from_ui(),
                    lm_studio_url=self.ini.lm_studio_url,
                    minimize_to_tray=self.ini.minimize_to_tray,
                )
            except ValueError:
                ini = self.ini
            if _health_ok(ini):
                if not self._server_ready:
                    self._server_ready = True
                    self._update_open_ui_button()
                self.status_text_var.set("Запущен")
                self._set_status_indicator("running")
            else:
                if self._server_ready:
                    self._server_ready = False
                    self._update_open_ui_button()
                self.status_text_var.set("Запуск…")
                self._set_status_indicator("loading")
        self.root.after(1500, self._poll_server)

    def _bind_version_update(self) -> None:
        self._version_label = f"v{APP_VERSION}"
        self.version_badge.configure(cursor="hand2")
        self.version_badge.bind("<Button-1>", lambda _e: self.check_update())
        self.version_badge.bind(
            "<Enter>",
            lambda _e: self.version_badge.configure(
                fg=self.theme.ACCENT_FG if not self._update_busy else self.theme.MUTED,
                bg=self.theme.ACCENT if not self._update_busy else self.theme.INPUT_BG,
                highlightbackground=self.theme.ACCENT if not self._update_busy else self.theme.CARD_BORDER,
            ),
        )
        self.version_badge.bind(
            "<Leave>",
            lambda _e: self._restore_version_badge_style(),
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
        if self.proc and self.proc.poll() is None:
            self.status_text_var.set("Запущен")
            self._set_status_indicator("running")
        else:
            self.status_text_var.set("Остановлен")
            self._set_status_indicator("stopped")

    def check_update(self) -> None:
        if not self._ui_ready or self._update_busy:
            return
        from app.portable.update import check_github_update

        self.status_text_var.set("Проверка обновлений…")
        self._set_status_indicator("loading")
        self._set_version_busy(True)
        self.root.update_idletasks()

        def work() -> None:
            info = check_github_update()
            self.root.after(0, lambda: self._show_update_result(info))

        threading.Thread(target=work, daemon=True).start()

    def _offer_install_update(self, info) -> None:
        if info.download_url and info.download_name:
            self._install_update(info)
            return
        if messagebox.askyesno("Обновление", f"Открыть страницу релиза {info.latest}?"):
            webbrowser.open(info.release_url)

    def _show_update_result(self, info) -> None:
        self._set_version_busy(False)
        self._restore_status_after_update()

        if info.error:
            self._pending_update = None
            messagebox.showerror(
                "Обновление",
                f"Не удалось проверить обновления:\n{info.error}\n\n{info.release_url}",
            )
            return

        if not info.newer:
            self._pending_update = None
            messagebox.showinfo(
                "Обновление",
                f"Установлена актуальная версия ({info.current}).",
            )
            return

        self._pending_update = info
        msg = f"Доступна версия {info.latest} (сейчас {info.current}).\n\nУстановить?"
        if messagebox.askyesno("Обновление", msg):
            self._offer_install_update(info)

    def _install_update(self, info) -> None:
        from app.portable.update import (
            download_update_zip,
            extract_portable_zip,
            pending_update_dir,
        )

        zip_path = self.root_dir / "_updates" / info.download_name
        staging_dir = pending_update_dir(self.root_dir, info.latest)
        self.status_text_var.set("Загрузка обновления…")
        self._set_status_indicator("loading")
        self._set_version_busy(True)

        def work() -> None:
            try:
                download_update_zip(info.download_url, zip_path)
                self.root.after(0, lambda: self._set_install_status("Установка обновления…"))
                staging_root = extract_portable_zip(zip_path, staging_dir)
                if os.name == "nt":
                    from app.portable.update import prepare_update_helper

                    prepare_update_helper(self.root_dir, staging_root)
                self.root.after(0, lambda: self._install_update_done(info, staging_root, None))
            except Exception as exc:
                self.root.after(0, lambda: self._install_update_done(info, None, exc))

        threading.Thread(target=work, daemon=True).start()

    def _set_install_status(self, text: str) -> None:
        self.status_text_var.set(text)
        self._set_status_indicator("loading")
        self.root.update_idletasks()

    def _install_update_done(self, info, staging_root: Path | None, error: Exception | None) -> None:
        self._set_version_busy(False)
        self._restore_status_after_update()
        if error is not None:
            messagebox.showerror("Обновление", f"Ошибка установки:\n{error}")
            return
        assert staging_root is not None
        self._pending_staging = staging_root
        msg = (
            f"Обновление до версии {info.latest} скачано и установлено.\n\n"
            "Перезапустить cfsmcp2?"
        )
        if messagebox.askyesno("Обновление", msg):
            self._restart_for_update(staging_root)
        else:
            hint = ""
            if os.name == "nt":
                hint = (
                    "\n\nЕсли авто-перезапуск не сработает: закройте cfsmcp2 "
                    "и запустите _updates\\update-portable.cmd."
                )
            messagebox.showinfo(
                "Обновление",
                "Перезапустите cfsmcp2 вручную — обновление будет завершено при запуске."
                + hint,
            )

    def _restart_for_update(self, staging_root: Path) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Обновление", "Остановить сервер и перезапустить cfsmcp2?"):
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
            messagebox.showerror("Обновление", f"Не удалось перезапустить:\n{exc}")
            return
        self.root.destroy()
        sys.exit(0)

    def _open_github_home(self) -> None:
        from app.portable.update import GITHUB_HOME

        webbrowser.open(GITHUB_HOME)

    def _hide_to_tray(self) -> None:
        if self._closing:
            return
        self.root.withdraw()
        self._ensure_tray()

    def _on_root_unmap(self, event: tk.Event) -> None:
        if self._closing or event.widget != self.root:
            return
        if str(self.root.state()) == "iconic":
            self.root.after(0, self._hide_to_tray)

    def _on_close_window(self) -> None:
        if not self._ui_ready:
            self._closing = True
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
            if not messagebox.askyesno("cfsmcp2", "Остановить сервер и выйти?"):
                return
        self._closing = True
        self.stop_server()
        self._stop_tray()
        if self.instance_lock is not None:
            self.instance_lock.release()
            self.instance_lock = None
        self.root.destroy()

    def _tray_image(self):
        from app.portable.tray_icon import load_tray_image

        return load_tray_image(_resource_path("cfs-mark.ico"))

    def _tray_title(self) -> str:
        running = self.proc is not None and self.proc.poll() is None
        state = "запущен" if running else "остановлен"
        return f"cfsmcp2 — {state}, {self.instance_name}"

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

        def show_window(icon, _item) -> None:
            self.root.after(0, self._show_window)

        def tray_start(icon, _item) -> None:
            self.root.after(0, self.start_server)

        def tray_stop(icon, _item) -> None:
            self.root.after(0, self.stop_server)

        def tray_open(icon, _item) -> None:
            self.root.after(0, self.open_ui)

        def tray_exit(icon, _item) -> None:
            self.root.after(0, self.exit_app)

        menu = pystray.Menu(
            pystray.MenuItem("Показать окно", show_window, default=True),
            pystray.MenuItem("Открыть UI", tray_open),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Старт", tray_start),
            pystray.MenuItem("Стоп", tray_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", tray_exit),
        )
        self.tray_icon = pystray.Icon("cfsmcp2", self._tray_image(), self._tray_title(), menu)

        def run_tray() -> None:
            assert self.tray_icon is not None
            self.tray_icon.run()

        self.tray_thread = threading.Thread(target=run_tray, daemon=True)
        self.tray_thread.start()
        self._update_tray_tooltip()

    def _show_window(self) -> None:
        try:
            self.root.state("normal")
        except tk.TclError:
            pass
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _stop_tray(self) -> None:
        if self.tray_icon is not None:
            self.tray_icon.stop()
            self.tray_icon = None

    def run(self) -> None:
        self.root.mainloop()


def _write_crash(text: str) -> None:
    try:
        path = portable_exe_dir() / "data" / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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


def _server_log_stream():
    """Attach stdout/stderr for windowed exe; avoid opening server.log twice."""
    stream = sys.stderr if sys.stderr is not None else sys.stdout
    if stream is not None:
        return stream
    for fd in (1, 2):
        try:
            return os.fdopen(fd, "a", closefd=False)
        except OSError:
            continue
    log_path = portable_exe_dir() / "data" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return open(log_path, "a", encoding="utf-8")


def _ensure_stdio() -> None:
    """Windowed PyInstaller exe has no console; uvicorn logging requires stdout/stderr."""
    stream = _server_log_stream()
    sys.stdout = stream
    sys.stderr = stream
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(stream)],
        force=True,
    )


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
    root_dir = portable_exe_dir()
    data_dir = default_data_dir()
    try:
        from app.portable.update import apply_pending_update_if_any

        if apply_pending_update_if_any(root_dir, wait_pid=os.getpid()):
            sys.exit(0)
    except Exception:
        log.exception("pending update apply failed")
    instance_lock = LauncherInstanceLock(root_dir)
    if not instance_lock.acquire():
        _show_fatal_error(
            "cfsmcp2 уже запущен из этого каталога.\n\n"
            f"Каталог: {root_dir.name}\n"
            f"Данные: {data_dir}"
        )
        return
    try:
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
