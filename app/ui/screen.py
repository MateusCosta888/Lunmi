"""Páginas internas; mantêm a janela, o histórico e o cancelamento vivos."""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from app.ui import theme


class Screen(tk.Frame):
    def __init__(self, parent: tk.Misc, title: str, on_back: Callable[[], object]) -> None:
        super().__init__(parent, background=theme.BG_CHAT)
        header = ttk.Frame(self, padding=(24, 12))
        header.pack(fill="x")
        self.back = ttk.Button(header, text="Voltar ao chat", command=on_back)
        self.back.pack(side="left", padx=(0, 20))
        ttk.Label(header, text=title, font=theme.F_SECTION).pack(side="left")
        self.canvas = tk.Canvas(self, background=theme.BG_CHAT, highlightthickness=0,
                                width=1, height=1)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        bar.pack(side="right", fill="y")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.configure(yscrollcommand=bar.set)
        self.body = ttk.Frame(self.canvas, padding=24)
        self.slot = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.canvas.bind("<Configure>", self._width)
        self.body.bind("<Configure>", lambda _: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.body.bind("<MouseWheel>", self._wheel)

    def _width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.slot, width=event.width)

    def _wheel(self, event: tk.Event) -> str:
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"

    def bind_scroll(self) -> None:
        stack = list(self.body.winfo_children())
        while stack:
            child = stack.pop()
            if not isinstance(child, (tk.Text, tk.Listbox, ttk.Treeview)):
                child.bind("<MouseWheel>", self._wheel)
            stack.extend(child.winfo_children())
