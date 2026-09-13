"""Launcher window: run and monitor config dumps (stage 6)."""

from __future__ import annotations

import logging
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import tkinter as tk
from tkinter import ttk

if TYPE_CHECKING:
    from app.portable.launcher import PortableLauncher

log = logging.getLogger("cfsmcp2.dump_window")

POLL_MS = 1500
_TERMINAL_STATES = frozenset({"ok", "failed", "cancelled"})


@dataclass
class _PendingAction:
    kind: str
    profile_ids: list[int] = field(default_factory=list)


class DumpWindowController:
    """Thin dump UI over REST; HTTP only from worker threads."""

    def __init__(self, launcher: PortableLauncher) -> None:
        self._launcher = launcher
        self._win: tk.Toplevel | None = None
        self._poll_after_id: str | None = None
        self._profiles: list[dict[str, Any]] = []
        self._runs: list[dict[str, Any]] = []
        self._profile_by_id: dict[int, dict[str, Any]] = {}
        self._checks: dict[int, tk.BooleanVar] = {}
        self._pending: _PendingAction | None = None
        self._run_state_cache: dict[int, str] = {}
        self._watch_runs: set[int] = set()
        self._busy = False
        self._server_panel: tk.Frame | None = None
        self._content_panel: tk.Frame | None = None
        self._current_panel: tk.Frame | None = None
        self._profiles_body: tk.Frame | None = None
        self._profiles_canvas: tk.Canvas | None = None
        self._profiles_canvas_window: int | None = None
        self._current_meta_var: tk.StringVar | None = None
        self._log_text: tk.Text | None = None
        self._batch_btn: tk.Widget | None = None
        self._start_server_btn: tk.Widget | None = None
        self._open_log_btn: tk.Widget | None = None
        self._active_run_id: int | None = None

    def _theme(self):
        return self._launcher.theme

    def _root(self) -> tk.Tk:
        return self._launcher.root

    def _api_base(self) -> str:
        ini = self._launcher._ini_for_health()
        from app.portable.ini import loopback_host

        host = loopback_host(ini.host)
        return f"http://{host}:{int(ini.port)}"

    def _server_ready(self) -> bool:
        return bool(self._launcher._server_ready and self._launcher._server_process_alive())

    def open_window(self) -> None:
        self._ensure_window()
        assert self._win is not None
        self._win.deiconify()
        self._win.lift()
        self._launcher._center_toplevel(self._win)
        self._refresh_view()

    def open_dumps_in_browser(self) -> None:
        if not self._server_ready():
            self._pending = _PendingAction("open_browser")
            self._ensure_window()
            self._refresh_view()
            return
        webbrowser.open(f"{self._api_base()}/#dumps")

    def tray_run_profile(self, profile_id: int) -> None:
        self._start_runs([profile_id])

    def on_server_ready(self) -> None:
        if self._pending is None:
            return
        action = self._pending
        self._pending = None
        if action.kind == "open_browser":
            webbrowser.open(f"{self._api_base()}/#dumps")
        elif action.kind == "run":
            self._post_run_batch(action.profile_ids)
        elif action.kind == "refresh":
            self._refresh_view()

    def should_poll(self) -> bool:
        if self._watch_runs:
            return True
        if self._win is not None:
            try:
                return bool(self._win.winfo_exists() and self._win.winfo_viewable())
            except tk.TclError:
                return False
        return False

    def poll_tick(self) -> None:
        if not self.should_poll():
            return
        if self._busy:
            self._schedule_poll()
            return
        if not self._server_ready():
            if self._win is not None:
                self._show_server_stopped()
            self._schedule_poll()
            return
        self._fetch_board_async()

    def ensure_profiles_for_tray(self) -> None:
        if self._profiles or not self._server_ready():
            return
        try:
            import httpx

            with httpx.Client(timeout=4.0) as client:
                r = client.get(f"{self._api_base()}/api/dump/profiles")
                r.raise_for_status()
                self._profiles = r.json()
                self._profile_by_id = {int(p["id"]): p for p in self._profiles}
        except Exception:
            log.debug("tray profile prefetch failed", exc_info=True)

    def tray_submenu_items(self) -> list[tuple[str, Callable[[], None]]]:
        self.ensure_profiles_for_tray()
        items: list[tuple[str, Callable[[], None]]] = []
        profiles = sorted(
            self._profiles,
            key=lambda p: str(p.get("last_run_at") or ""),
            reverse=True,
        )[:5]
        for prof in profiles:
            pid = int(prof["id"])
            name = str(prof.get("name") or f"#{pid}")
            items.append((name, lambda p=pid: self._root().after(0, lambda: self.tray_run_profile(p))))
        return items

    def _schedule_poll(self) -> None:
        if self._poll_after_id is not None:
            try:
                self._root().after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
        self._poll_after_id = self._root().after(POLL_MS, self._poll_tick)

    def _poll_tick(self) -> None:
        self._poll_after_id = None
        self.poll_tick()

    def _ensure_window(self) -> None:
        if self._win is not None:
            try:
                if self._win.winfo_exists():
                    return
            except tk.TclError:
                pass
        theme = self._theme()
        win = tk.Toplevel(self._root())
        win.title("Выгрузка в файлы")
        win.transient(self._root())
        win.configure(bg=theme.SURFACE)
        win.geometry("720x520")
        win.minsize(560, 400)
        win.protocol("WM_DELETE_WINDOW", self._close_window)

        outer = tk.Frame(win, bg=theme.SURFACE, padx=theme.sp(16), pady=theme.sp(12))
        outer.pack(fill=tk.BOTH, expand=True)

        self._server_panel = tk.Frame(outer, bg=theme.SURFACE)
        tk.Label(
            self._server_panel,
            text="Сервер не запущен",
            font=theme.font_body("bold"),
            fg=theme.FG,
            bg=theme.SURFACE,
        ).pack(anchor="w")
        tk.Label(
            self._server_panel,
            text="Профили выгрузки доступны после запуска сервера cfsmcp2.",
            font=theme.font_sm(),
            fg=theme.MUTED,
            bg=theme.SURFACE,
            justify=tk.LEFT,
            wraplength=640,
        ).pack(anchor="w", pady=(theme.sp(6), theme.sp(12)))
        self._start_server_btn = theme.make_button(
            self._server_panel,
            "Запустить сервер и выгрузить",
            self._on_start_server_and_continue,
            kind="primary",
        )
        self._start_server_btn.pack(anchor="w")

        self._content_panel = tk.Frame(outer, bg=theme.SURFACE)

        self._current_panel = tk.Frame(self._content_panel, bg=theme.CARD, padx=theme.sp(10), pady=theme.sp(8))
        self._current_panel.pack(fill=tk.X, pady=(0, theme.sp(10)))
        tk.Label(
            self._current_panel,
            text="Текущий прогон",
            font=theme.font_sm("bold"),
            fg=theme.MUTED,
            bg=theme.CARD,
        ).pack(anchor="w")
        self._current_meta_var = tk.StringVar(value="")
        tk.Label(
            self._current_panel,
            textvariable=self._current_meta_var,
            font=theme.font_body(),
            fg=theme.FG,
            bg=theme.CARD,
            justify=tk.LEFT,
            wraplength=660,
        ).pack(anchor="w", pady=(theme.sp(4), theme.sp(6)))
        log_frame = tk.Frame(self._current_panel, bg=theme.CARD)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self._log_text = tk.Text(
            log_frame,
            height=6,
            font=theme.font_mono(),
            bg=theme.INPUT_BG,
            fg=theme.FG,
            relief=tk.FLAT,
            wrap=tk.WORD,
            state=tk.DISABLED,
        )
        scroll = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=scroll.set)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        cancel_row = tk.Frame(self._current_panel, bg=theme.CARD)
        cancel_row.pack(fill=tk.X, pady=(theme.sp(6), 0))
        theme.make_button(cancel_row, "Отменить", self._cancel_active_run, kind="footer").pack(side=tk.LEFT)

        header = tk.Frame(self._content_panel, bg=theme.SURFACE)
        header.pack(fill=tk.X)
        tk.Label(header, text="Профили", font=theme.font_sm("bold"), fg=theme.MUTED, bg=theme.SURFACE).pack(
            side=tk.LEFT
        )

        list_wrap = tk.Frame(self._content_panel, bg=theme.SURFACE)
        list_wrap.pack(fill=tk.BOTH, expand=True, pady=(theme.sp(6), theme.sp(8)))
        canvas = tk.Canvas(list_wrap, bg=theme.INPUT_BG, highlightthickness=1, highlightbackground=theme.CARD_BORDER)
        scroll_y = ttk.Scrollbar(list_wrap, orient=tk.VERTICAL, command=canvas.yview)
        self._profiles_body = tk.Frame(canvas, bg=theme.INPUT_BG)
        self._profiles_canvas = canvas
        self._profiles_body.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        self._profiles_canvas_window = canvas.create_window((0, 0), window=self._profiles_body, anchor="nw")

        def _on_profiles_canvas_configure(event: tk.Event) -> None:
            if self._profiles_canvas_window is not None:
                canvas.itemconfigure(self._profiles_canvas_window, width=event.width)

        canvas.bind("<Configure>", _on_profiles_canvas_configure)
        canvas.configure(yscrollcommand=scroll_y.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)

        btn_row = tk.Frame(self._content_panel, bg=theme.SURFACE)
        btn_row.pack(fill=tk.X, pady=(0, theme.sp(6)))
        self._batch_btn = theme.make_button(btn_row, "Выгрузить выделенные", self._run_selected, kind="primary")
        self._batch_btn.pack(side=tk.LEFT)
        theme.make_button(btn_row, "Настроить профили в браузере", self.open_dumps_in_browser, kind="footer").pack(
            side=tk.LEFT, padx=(theme.sp(8), 0)
        )
        self._open_log_btn = theme.make_button(btn_row, "Открыть лог", self._open_active_log, kind="footer")
        self._open_log_btn.pack(side=tk.RIGHT)

        self._win = win
        self._launcher._center_toplevel(win)
        self._refresh_view()

    def _close_window(self) -> None:
        if self._win is not None:
            try:
                self._win.withdraw()
            except tk.TclError:
                pass

    def _refresh_view(self) -> None:
        if self._win is None:
            return
        if not self._server_ready():
            self._show_server_stopped()
            return
        self._server_panel.pack_forget()
        self._content_panel.pack(fill=tk.BOTH, expand=True)
        self._fetch_board_async()

    def _show_server_stopped(self) -> None:
        if self._win is None:
            return
        self._content_panel.pack_forget()
        self._server_panel.pack(fill=tk.BOTH, expand=True)
        label = "Запустить сервер и выгрузить"
        if self._pending and self._pending.kind == "run":
            label = "Запустить сервер и выгрузить"
        if self._start_server_btn is not None:
            self._theme().set_button_enabled(self._start_server_btn, not self._launcher._server_process_alive())

    def _on_start_server_and_continue(self) -> None:
        if self._server_ready():
            self.on_server_ready()
            self._refresh_view()
            return
        if not self._launcher._server_process_alive():
            if self._pending is None:
                self._pending = _PendingAction("refresh")
            self._launcher.start_server()
        self._refresh_view()
        self._schedule_poll()

    def _fetch_board_async(self) -> None:
        if self._busy:
            return
        self._busy = True

        def work() -> None:
            err: str | None = None
            profiles: list[dict] = []
            runs: list[dict] = []
            try:
                import httpx

                base = self._api_base()
                with httpx.Client(timeout=8.0) as client:
                    pr = client.get(f"{base}/api/dump/profiles")
                    pr.raise_for_status()
                    profiles = pr.json()
                    rr = client.get(f"{base}/api/dump/runs", params={"limit": 50})
                    rr.raise_for_status()
                    runs = rr.json()
            except Exception as exc:
                err = str(exc)
                log.debug("dump board fetch failed", exc_info=True)
            self._root().after(0, lambda: self._apply_board(profiles, runs, err))

        threading.Thread(target=work, daemon=True).start()

    def _apply_board(self, profiles: list[dict], runs: list[dict], err: str | None) -> None:
        self._busy = False
        if err and self._win and self._win.winfo_viewable():
            self._launcher._show_error("Выгрузка", f"Не удалось загрузить данные:\n{err}")
        self._profiles = profiles
        self._runs = runs
        self._profile_by_id = {int(p["id"]): p for p in profiles}
        self._detect_finished_runs(runs)
        self._render_profiles()
        self._render_current_run()
        self._sync_batch_button()
        self._launcher._refresh_dump_tray_menu()
        if self.should_poll():
            self._schedule_poll()

    def _detect_finished_runs(self, runs: list[dict]) -> None:
        for run in runs:
            rid = int(run["id"])
            state = str(run.get("state") or "")
            prev = self._run_state_cache.get(rid)
            if prev in ("running", "queued") and state in _TERMINAL_STATES:
                if rid in self._watch_runs or (self._win and self._win.winfo_viewable()):
                    self._notify_run_finished(run)
            if state in ("running", "queued"):
                self._watch_runs.add(rid)
            elif state in _TERMINAL_STATES and rid in self._watch_runs:
                self._watch_runs.discard(rid)
            self._run_state_cache[rid] = state

    def _notify_run_finished(self, run: dict) -> None:
        prof = self._profile_by_id.get(int(run.get("profile_id") or 0))
        name = str(prof.get("name") if prof else f"#{run.get('profile_id')}")
        elapsed = self._format_elapsed(run)
        state = str(run.get("state") or "")
        ingest = str(run.get("ingest_state") or "")
        if state == "ok" and ingest == "blocked":
            msg = f"{name} — выгрузка прошла, контекст не обновлён"
            reason = str(run.get("error_reason") or "").strip()
            if reason:
                msg = f"{msg}\n{reason}"
        elif state == "ok":
            msg = f"{name} — выгружено за {elapsed}" if elapsed else f"{name} — выгружено"
            if run.get("bootstrap_note"):
                msg += "\nпервый прогон: полная выгрузка"
        elif state == "cancelled":
            msg = f"{name} — отменено"
        else:
            reason = str(run.get("error_reason") or "ошибка")
            msg = f"{name} — {reason}"
        self._launcher._tray_notify(msg, title="Выгрузка cfsmcp2")

    @staticmethod
    def _parse_sqlite_utc(raw: str):
        from datetime import datetime, timezone

        s = str(raw or "").strip().replace("T", " ")[:19]
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _format_elapsed(self, run: dict) -> str:
        t0 = self._parse_sqlite_utc(str(run.get("started_at") or ""))
        t1 = self._parse_sqlite_utc(str(run.get("finished_at") or ""))
        if t0 is None or t1 is None:
            return ""
        sec = max(0, int((t1 - t0).total_seconds()))
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _last_run_for_profile(self, profile_id: int) -> dict | None:
        for run in self._runs:
            if int(run.get("profile_id") or 0) == profile_id:
                return run
        return None

    def _target_label(self, prof: dict) -> str:
        if str(prof.get("target_type") or "") == "extension":
            ext = str(prof.get("extension_name") or "").strip()
            return f"расширение {ext}" if ext else "расширение"
        return "конфигурация"

    def _last_run_label(self, prof: dict) -> str:
        run = self._last_run_for_profile(int(prof["id"]))
        if not run:
            last = str(prof.get("last_run_state") or "")
            return last or "—"
        return self._run_status_label(run)

    def _run_status_label(self, run: dict) -> str:
        state = str(run.get("state") or "")
        ingest = str(run.get("ingest_state") or "")
        if state == "ok" and ingest == "blocked":
            return "ok · контекст не обновлён"
        if state == "ok":
            note = " · первый прогон: полная выгрузка" if run.get("bootstrap_note") else ""
            return f"ok{note}"
        if state == "running":
            return "выполняется"
        if state == "queued":
            return "в очереди"
        if state == "cancelled":
            return "отменено"
        if state == "failed":
            return str(run.get("error_reason") or "ошибка")
        return state or "—"

    def _render_profiles(self) -> None:
        if self._profiles_body is None:
            return
        for child in self._profiles_body.winfo_children():
            child.destroy()
        theme = self._theme()
        if not self._profiles:
            tk.Label(
                self._profiles_body,
                text="Нет профилей. Настройте их в браузере.",
                font=theme.font_sm(),
                fg=theme.MUTED,
                bg=theme.INPUT_BG,
                padx=8,
                pady=12,
            ).pack(anchor="w")
            return
        for prof in self._profiles:
            pid = int(prof["id"])
            row = tk.Frame(self._profiles_body, bg=theme.INPUT_BG, pady=4, padx=6)
            row.pack(fill=tk.X)
            if pid not in self._checks:
                self._checks[pid] = tk.BooleanVar(value=False)
            var = self._checks[pid]
            cb = tk.Checkbutton(
                row,
                variable=var,
                bg=theme.INPUT_BG,
                activebackground=theme.INPUT_BG,
                command=self._sync_batch_button,
            )
            cb.pack(side=tk.LEFT)
            text = (
                f"{prof.get('name', '')}  ·  {self._target_label(prof)}  ·  "
                f"{prof.get('out_dir', '')}  ·  {self._last_run_label(prof)}"
            )
            tk.Label(
                row,
                text=text,
                font=theme.font_sm(),
                fg=theme.FG,
                bg=theme.INPUT_BG,
                anchor="w",
                justify=tk.LEFT,
                wraplength=520,
            ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 8))
            theme.make_button(row, "▶", lambda p=pid: self._start_runs([p]), kind="footer").pack(side=tk.RIGHT)
        tk.Frame(self._profiles_body, bg=theme.INPUT_BG, height=theme.sp(12)).pack(fill=tk.X)
        if self._profiles_canvas is not None:
            self._profiles_body.update_idletasks()
            self._profiles_canvas.configure(scrollregion=self._profiles_canvas.bbox("all"))

    def _active_run(self) -> dict | None:
        for run in self._runs:
            if str(run.get("state")) in ("running", "queued"):
                return run
        return None

    def _render_current_run(self) -> None:
        if self._current_panel is None or self._current_meta_var is None or self._log_text is None:
            return
        run = self._active_run()
        if not run:
            self._current_panel.pack_forget()
            self._active_run_id = None
            return
        self._current_panel.pack(fill=tk.X, pady=(0, self._theme().sp(10)))
        self._active_run_id = int(run["id"])
        prof = self._profile_by_id.get(int(run.get("profile_id") or 0))
        name = str(prof.get("name") if prof else f"#{run.get('profile_id')}")
        elapsed = self._format_elapsed(run) if run.get("finished_at") else self._format_elapsed_live(run)
        fc = int(run.get("file_count") or 0)
        status = self._run_status_label(run)
        self._current_meta_var.set(f"{name}  ·  {status}  ·  {elapsed or '…'}  ·  файлов: {fc}")
        tail = str(run.get("log_tail") or "")
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.insert(tk.END, tail)
        self._log_text.configure(state=tk.DISABLED)
        if self._open_log_btn is not None:
            path = str(run.get("log_path") or "")
            self._theme().set_button_enabled(self._open_log_btn, bool(path))

    def _format_elapsed_live(self, run: dict) -> str:
        from datetime import datetime, timezone

        t0 = self._parse_sqlite_utc(str(run.get("started_at") or ""))
        if t0 is None:
            return ""
        sec = max(0, int((datetime.now(timezone.utc) - t0).total_seconds()))
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _sync_batch_button(self) -> None:
        if self._batch_btn is None:
            return
        selected = sum(1 for v in self._checks.values() if v.get())
        self._theme().set_button_enabled(self._batch_btn, selected > 0 and self._server_ready())

    def _selected_profile_ids(self) -> list[int]:
        return [pid for pid, var in self._checks.items() if var.get()]

    def _start_runs(self, profile_ids: list[int]) -> None:
        if not profile_ids:
            return
        if not self._server_ready():
            self._pending = _PendingAction("run", list(profile_ids))
            self.open_window()
            return
        self._post_run_batch(profile_ids)

    def _run_selected(self) -> None:
        ids = self._selected_profile_ids()
        if ids:
            self._start_runs(ids)

    def _post_run_batch(self, profile_ids: list[int]) -> None:
        self._busy = True

        def work() -> None:
            err: str | None = None
            run_ids: list[int] = []
            try:
                import httpx

                base = self._api_base()
                with httpx.Client(timeout=15.0) as client:
                    if len(profile_ids) == 1:
                        r = client.post(f"{base}/api/dump/profiles/{profile_ids[0]}/run")
                        r.raise_for_status()
                        run_ids = [int(r.json()["run_id"])]
                    else:
                        r = client.post(f"{base}/api/dump/run-batch", json={"profile_ids": profile_ids})
                        r.raise_for_status()
                        run_ids = [int(x["run_id"]) for x in r.json()]
            except Exception as exc:
                err = str(exc)
            self._root().after(0, lambda: self._runs_started(run_ids, err))

        threading.Thread(target=work, daemon=True).start()

    def _runs_started(self, run_ids: list[int], err: str | None) -> None:
        self._busy = False
        if err:
            self._launcher._show_error("Выгрузка", f"Не удалось запустить:\n{err}")
            return
        for rid in run_ids:
            self._watch_runs.add(rid)
            self._run_state_cache[rid] = "queued"
        self._fetch_board_async()

    def _cancel_active_run(self) -> None:
        run = self._active_run()
        if not run:
            return
        run_id = int(run["id"])
        self._busy = True

        def work() -> None:
            err: str | None = None
            try:
                import httpx

                base = self._api_base()
                with httpx.Client(timeout=10.0) as client:
                    r = client.post(f"{base}/api/dump/runs/{run_id}/cancel")
                    r.raise_for_status()
            except Exception as exc:
                err = str(exc)
            self._root().after(0, lambda: self._cancel_done(err))

        threading.Thread(target=work, daemon=True).start()

    def _cancel_done(self, err: str | None) -> None:
        self._busy = False
        if err:
            self._launcher._show_error("Выгрузка", f"Не удалось отменить:\n{err}")
        self._fetch_board_async()

    def _open_active_log(self) -> None:
        run = self._active_run()
        if not run:
            for r in self._runs:
                if r.get("log_path"):
                    run = r
                    break
        if not run:
            return
        path = str(run.get("log_path") or "")
        if path:
            self._launcher._open_path(Path(path))
