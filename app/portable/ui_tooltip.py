"""Small hover tooltips for Tk widgets."""

from __future__ import annotations

import tkinter as tk


class Tooltip:
    _instances: list[Tooltip] = []

    def __init__(
        self,
        widget: tk.Misc,
        text: str,
        *,
        delay_ms: int = 500,
        bg: str = "#272F42",
        fg: str = "#F8FAFC",
        border: str = "#475569",
    ) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.bg = bg
        self.fg = fg
        self.border = border
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")
        widget.bind("<Destroy>", self._on_widget_destroy, add="+")
        Tooltip._instances.append(self)

    @classmethod
    def hide_all(cls) -> None:
        for tip in list(cls._instances):
            tip._on_leave()

    def set_text(self, text: str) -> None:
        self.text = text

    def _root_viewable(self) -> bool:
        try:
            if not self.widget.winfo_exists():
                return False
            root = self.widget.winfo_toplevel()
            return bool(root.winfo_viewable())
        except tk.TclError:
            return False

    def _on_enter(self, _event: tk.Event) -> None:
        if not self._root_viewable():
            return
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _on_leave(self, _event: tk.Event | None = None) -> None:
        self._cancel()
        self._hide()

    def _on_widget_destroy(self, _event: tk.Event) -> None:
        self._on_leave()
        try:
            Tooltip._instances.remove(self)
        except ValueError:
            pass

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self) -> None:
        self._after_id = None
        if not self.text or not self._root_viewable():
            return
        self._hide()
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.configure(bg=self.border)
        label = tk.Label(
            tip,
            text=self.text,
            justify=tk.LEFT,
            bg=self.bg,
            fg=self.fg,
            relief=tk.FLAT,
            font=("Segoe UI", 9),
            padx=8,
            pady=4,
        )
        label.pack(padx=1, pady=1)
        tip.update_idletasks()
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tip.geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self) -> None:
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None
