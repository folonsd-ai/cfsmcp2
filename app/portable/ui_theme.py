"""Visual theme for portable launcher — aligned with app/static/ide-console.css."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk


def _pick_family(
    available: dict[str, str],
    candidates: tuple[str, ...],
    fallback: str,
) -> str:
    for candidate in candidates:
        hit = available.get(candidate.lower())
        if hit:
            return hit
    return fallback


class LauncherTheme:
    BG = "#0F172A"
    SURFACE = "#151D2E"
    CARD = "#1B2336"
    CARD_BORDER = "#334155"
    FG = "#F8FAFC"
    MUTED = "#94A3B8"
    ACCENT = "#22C55E"
    ACCENT_HOVER = "#3FDC79"
    ACCENT_FG = "#0F172A"
    DANGER = "#EF4444"
    DANGER_HOVER = "#F87171"
    INFO = "#5B9BFF"
    INPUT_BG = "#272F42"
    INPUT_BORDER = "#475569"
    WARNING = "#F5A524"
    STATUS_STOPPED_BG = "#1A2234"
    STATUS_LOADING_BG = "#2A2415"
    STATUS_RUNNING_BG = "#16271E"
    STATUS_UNRESPONSIVE_BG = "#2A2415"
    STATUS_ERROR_BG = "#2A1A1D"
    STATUS_FLASH_BG = "#1A2838"
    BTN_PRIMARY_WEIGHT = 3
    BTN_SECONDARY_WEIGHT = 2
    BTN_DISABLED_FG = "#64748B"
    BTN_DISABLED_BORDER = "#2A3448"
    BTN_DISABLED_PRIMARY_BG = "#1A2E24"
    BTN_DISABLED_DANGER_BG = "#2E1A1F"
    BTN_DISABLED_SECONDARY_BG = "#1A2234"

    SCALE = 1.0
    WIN_WIDTH = 400
    WIN_MIN_HEIGHT = 380
    PAD_X = 16
    BTN_HEIGHT = 34

    SANS = "Segoe UI"
    MONO = "Consolas"
    _SANS_CANDIDATES = ("IBM Plex Sans", "Segoe UI Variable Text", "Segoe UI", "Tahoma")
    _MONO_CANDIDATES = ("JetBrains Mono", "Cascadia Mono", "Consolas", "Courier New")

    @classmethod
    def set_scale(cls, root: tk.Misc) -> None:
        try:
            cls.SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            cls.SCALE = 1.0

    @classmethod
    def sp(cls, px: int) -> int:
        return max(px, int(round(px * cls.SCALE)))

    @classmethod
    def init_fonts(cls, root: tk.Misc) -> None:
        available = {name.lower(): name for name in tkfont.families(root)}
        cls.SANS = _pick_family(available, cls._SANS_CANDIDATES, "Segoe UI")
        cls.MONO = _pick_family(available, cls._MONO_CANDIDATES, "Consolas")

    @classmethod
    def font_body(cls, weight: str = "normal") -> tuple[str, int, str]:
        return (cls.SANS, 10, weight)

    @classmethod
    def font_sm(cls, weight: str = "normal") -> tuple[str, int, str]:
        return (cls.SANS, 9, weight)

    @classmethod
    def font_btn(cls, weight: str = "normal") -> tuple[str, int, str]:
        return (cls.SANS, 10, weight)

    @classmethod
    def font_title(cls) -> tuple[str, int, str]:
        return (cls.SANS, 13, "bold")

    @classmethod
    def font_mono(cls, weight: str = "normal") -> tuple[str, int, str]:
        return (cls.MONO, 9, weight)

    @classmethod
    def apply_ttk(cls, root: tk.Misc) -> ttk.Style:
        cls.init_fonts(root)
        cls.set_scale(root)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background=cls.CARD, foreground=cls.FG, font=cls.font_body())
        style.configure("TFrame", background=cls.CARD)
        style.configure("Root.TFrame", background=cls.BG)
        style.configure("Header.TFrame", background=cls.SURFACE)
        style.configure("TLabel", background=cls.CARD, foreground=cls.FG, font=cls.font_body())
        style.configure("HeaderTitle.TLabel", background=cls.SURFACE, foreground=cls.FG, font=cls.font_title())
        style.configure("HeaderMuted.TLabel", background=cls.SURFACE, foreground=cls.MUTED, font=cls.font_sm())
        style.configure(
            "TRadiobutton",
            background=cls.CARD,
            foreground=cls.FG,
            font=cls.font_body(),
            padding=(2, 3),
        )
        style.map(
            "TRadiobutton",
            background=[("active", cls.CARD), ("disabled", cls.CARD)],
            foreground=[("disabled", cls.MUTED)],
            indicatorcolor=[("selected", cls.ACCENT)],
        )
        style.configure(
            "TEntry",
            fieldbackground=cls.INPUT_BG,
            foreground=cls.FG,
            bordercolor=cls.INPUT_BORDER,
            lightcolor=cls.INPUT_BORDER,
            darkcolor=cls.INPUT_BORDER,
            insertcolor=cls.FG,
            padding=(8, 6),
            font=cls.font_body(),
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", cls.SURFACE)],
            foreground=[("disabled", cls.MUTED)],
            bordercolor=[("focus", cls.ACCENT)],
        )
        style.configure(
            "TCheckbutton",
            background=cls.CARD,
            foreground=cls.FG,
            font=cls.font_body(),
            focuscolor=cls.CARD,
            borderwidth=0,
            padding=(0, 2),
        )
        style.map(
            "TCheckbutton",
            background=[("active", cls.CARD), ("selected", cls.CARD), ("disabled", cls.CARD)],
            foreground=[("active", cls.FG), ("selected", cls.FG), ("disabled", cls.MUTED)],
            indicatorcolor=[("selected", cls.ACCENT), ("pressed", cls.ACCENT)],
        )
        return style

    @classmethod
    def make_checkbutton(
        cls,
        parent: tk.Misc,
        text: str,
        variable: tk.Variable,
        command,
    ) -> tk.Checkbutton:
        """Dark-theme checkbox without ttk hover flash on Windows."""
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=command,
            bg=cls.CARD,
            fg=cls.FG,
            selectcolor=cls.INPUT_BG,
            activebackground=cls.CARD,
            activeforeground=cls.FG,
            highlightthickness=0,
            bd=0,
            anchor="w",
            font=cls.font_body(),
            cursor="hand2",
        )

    @classmethod
    def card(cls, parent: tk.Misc, *, pady: int = 6) -> tuple[tk.Frame, tk.Frame]:
        outer = tk.Frame(
            parent,
            bg=cls.CARD,
            highlightthickness=1,
            highlightbackground=cls.CARD_BORDER,
            highlightcolor=cls.CARD_BORDER,
        )
        outer.pack(fill=tk.X, padx=cls.sp(cls.PAD_X), pady=cls.sp(pady))
        inner = tk.Frame(outer, bg=cls.CARD, padx=cls.sp(12), pady=cls.sp(10))
        inner.pack(fill=tk.X)
        return outer, inner

    @classmethod
    def popup_menu(cls, parent: tk.Misc) -> tk.Menu:
        return tk.Menu(
            parent,
            tearoff=0,
            bg=cls.INPUT_BG,
            fg=cls.FG,
            activebackground=cls.ACCENT,
            activeforeground=cls.ACCENT_FG,
            borderwidth=0,
            relief=tk.FLAT,
        )

    @classmethod
    def section_title(cls, parent: tk.Misc, text: str) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            font=cls.font_sm("bold"),
            fg=cls.MUTED,
            bg=cls.CARD,
            anchor="w",
        )

    @classmethod
    def _button_palette(cls, kind: str) -> tuple[str, str, str, str, str]:
        if kind == "primary":
            return cls.ACCENT, cls.ACCENT_FG, cls.ACCENT_HOVER, cls.ACCENT, "bold"
        if kind == "danger":
            return cls.DANGER, cls.FG, cls.DANGER_HOVER, cls.DANGER, "normal"
        if kind == "stop":
            return cls.INPUT_BG, cls.FG, cls.DANGER, cls.CARD_BORDER, "bold"
        if kind == "footer":
            return cls.INPUT_BG, cls.FG, cls.CARD_BORDER, cls.CARD_BORDER, "bold"
        return cls.INPUT_BG, cls.FG, cls.CARD_BORDER, cls.CARD_BORDER, "normal"

    @classmethod
    def _wire_button_hover(cls, btn: tk.Button) -> None:
        btn.unbind("<Enter>")
        btn.unbind("<Leave>")

        def _on_enter(_e: tk.Event) -> None:
            if str(btn["state"]) != tk.DISABLED:
                btn.configure(bg=btn._theme_hover)  # type: ignore[attr-defined]

        def _on_leave(_e: tk.Event) -> None:
            if str(btn["state"]) != tk.DISABLED:
                btn.configure(bg=btn._theme_bg)  # type: ignore[attr-defined]

        btn.bind("<Enter>", _on_enter)
        btn.bind("<Leave>", _on_leave)

    @classmethod
    def make_button(
        cls,
        parent: tk.Misc,
        text: str,
        command,
        *,
        kind: str = "secondary",
    ) -> tk.Button:
        bg, fg, hover, border, weight = cls._button_palette(kind)

        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
            disabledforeground=cls.MUTED,
            font=cls.font_btn(weight),
            relief=tk.FLAT,
            padx=cls.sp(10),
            pady=cls.sp(8),
            cursor="hand2",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
        )
        btn._theme_kind = kind  # type: ignore[attr-defined]
        btn._theme_bg = bg  # type: ignore[attr-defined]
        btn._theme_fg = fg  # type: ignore[attr-defined]
        btn._theme_border = border  # type: ignore[attr-defined]
        btn._theme_hover = hover  # type: ignore[attr-defined]
        cls._wire_button_hover(btn)
        return btn

    @classmethod
    def set_button_kind(cls, btn: tk.Button, kind: str) -> None:
        bg, fg, hover, border, weight = cls._button_palette(kind)
        btn._theme_kind = kind  # type: ignore[attr-defined]
        btn._theme_bg = bg  # type: ignore[attr-defined]
        btn._theme_fg = fg  # type: ignore[attr-defined]
        btn._theme_border = border  # type: ignore[attr-defined]
        btn._theme_hover = hover  # type: ignore[attr-defined]
        btn.configure(font=cls.font_btn(weight))
        enabled = str(btn["state"]) != tk.DISABLED
        cls.set_button_enabled(btn, enabled)
        cls._wire_button_hover(btn)

    @classmethod
    def _disabled_style(cls, kind: str) -> tuple[str, str, str]:
        if kind == "primary":
            return cls.BTN_DISABLED_PRIMARY_BG, cls.BTN_DISABLED_FG, cls.BTN_DISABLED_BORDER
        if kind == "danger":
            return cls.BTN_DISABLED_DANGER_BG, cls.BTN_DISABLED_FG, cls.BTN_DISABLED_BORDER
        if kind in ("stop", "footer"):
            return cls.BTN_DISABLED_SECONDARY_BG, cls.BTN_DISABLED_FG, cls.BTN_DISABLED_BORDER
        return cls.BTN_DISABLED_SECONDARY_BG, cls.BTN_DISABLED_FG, cls.BTN_DISABLED_BORDER

    @classmethod
    def set_button_enabled(cls, btn: tk.Button, enabled: bool) -> None:
        kind = getattr(btn, "_theme_kind", "secondary")
        if enabled:
            btn.configure(
                state=tk.NORMAL,
                bg=btn._theme_bg,  # type: ignore[attr-defined]
                fg=btn._theme_fg,  # type: ignore[attr-defined]
                highlightbackground=btn._theme_border,  # type: ignore[attr-defined]
                cursor="hand2",
            )
            return
        disabled_bg, disabled_fg, disabled_border = cls._disabled_style(kind)
        btn.configure(
            state=tk.DISABLED,
            bg=disabled_bg,
            fg=disabled_fg,
            highlightbackground=disabled_border,
            cursor="arrow",
        )
