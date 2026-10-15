"""Casca da interface Lunmi: sidebar, barra de topo, coluna central e painel direito.

Quatro decisões atravessam o módulo:

- **Breakpoint em pixel lógico.** A especificação dá larguras em pixels a 100%. Comparar a largura
  física da janela contra esse número faria o layout trocar de coluna em 150% sem ninguém pedir;
  tudo decide em lógico e desenha em físico (`theme.display_scale`).
- **Superfície desenha, Tk organiza.** `theme.Surface` só pinta o fundo arredondado e hospeda um
  frame; o conteúdo de dentro é `pack`/`grid` normal. Ícone é um mini-`Canvas` filho, não traço
  sobre a superfície — assim ele acompanha o layout em vez de brigar com ele.
- **Controle desenhado onde o tema nativo não obedece.** `vista` ignora cor de botão; um app navy
  com botões cinza-claros não é o app da referência. Linha de navegação, pill, toggle e cartão são
  `Surface`, mas com foco de teclado real, `Enter`/`Espaço`, e os diálogos de arquivo e a moldura
  da janela continuam do Windows.
- **Nada é decorativo-funcional.** Uma rota sem backend abre aviso com motivo; nenhum botão
  desenhado finge capacidade. Ver `docs/design/lunmi/STATUS.md`.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Literal, TypeVar
from tkinter import ttk

from app.ui import glyphs, theme


class BrainButton(ttk.Button):
    def __init__(self, parent: tk.Misc, **kwargs: object) -> None:
        self._chevron = tk.PhotoImage(master=parent, width=12, height=8)
        for x in range(6):
            self._chevron.put(theme.TEXT_SECONDARY, to=(x, x))
            self._chevron.put(theme.TEXT_SECONDARY, to=(11 - x, x))
        super().__init__(parent, image=self._chevron, compound="right", **kwargs)  # type: ignore[arg-type]

    def get_text(self) -> str:
        return str(self.cget("text"))

    def set_text(self, value: str) -> None:
        self.configure(text=value)


class IconCanvas(tk.Canvas):
    """Glifo desenhado num widget próprio, para que o gerenciador de layout o mova.

    O tamanho fica num atributo com nome próprio: `Canvas.size` é método do widget (devolve
    largura e altura), e atribuir a ele silenciosamente quebraria `winfo` de quem chamasse.
    """

    def __init__(self, master: tk.Misc, name: str, size: int, color: str,
                 background: str) -> None:
        super().__init__(master, width=size, height=size, background=background,
                         highlightthickness=0, bd=0)
        self.glyph_name = name
        self.glyph_size = size
        self._tag = glyphs.draw(self, name, size / 2, size / 2, size, color)

    def set_color(self, color: str) -> None:
        glyphs.erase(self, self._tag)
        self._tag = glyphs.draw(self, self.glyph_name, self.glyph_size / 2, self.glyph_size / 2,
                                self.glyph_size, color)

    def set_background(self, background: str) -> None:
        self.configure(background=background)


class Pressable(theme.Surface):
    """Superfície arredondada que é um controle: hover, pressionado, foco e teclado reais."""

    def __init__(self, master: tk.Misc, *, fill: str, hover: str, active: str = "",
                 outline: str = "", radius: int = theme.RADIUS_CONTROL,
                 padding: tuple[int, ...] = (0, 0, 0, 0),
                 command: Callable[[], None] | None = None, name: str = "",
                 takefocus: bool = True, fit: bool = False) -> None:
        super().__init__(master, fill=fill, outline=outline, radius=radius,
                         background=theme.BG_WINDOW, padding=padding, name=name, fit=fit)
        self._idle_fill, self._hover_fill, self._active_fill = fill, hover, active or fill
        self._idle_outline = outline
        self._command = command
        self._enabled = True
        self._click_targets: list[tk.Widget] = []
        self.configure(takefocus=1 if takefocus else 0, cursor="hand2" if command else "")
        self.bind("<FocusIn>", self._on_focus, add="+")
        self.bind("<FocusOut>", self._on_blur, add="+")
        self.bind("<KeyRelease-Return>", self._activate, add="+")
        self.bind("<KeyRelease-space>", self._activate, add="+")
        self.bind("<Key-Return>", lambda _e: "break")
        self.bind("<Key-space>", lambda _e: "break")

    def install_click(self) -> None:
        """Clique em QUALQUER peça do controle aciona uma vez. Chame ao fim do `__init__` do filho.

        Tk não propaga evento de mouse pelos bindtags dos ancestrais widgets: ligar só no Canvas
        deixava a linha de navegação morta quando o ponteiro caía no rótulo ou no ícone, que é o
        caso normal de uso. Cada descendente recebe o par de ligações, e a decisão de "soltou
        dentro" é por posição absoluta — a coordenada local do filho não vale para o pai.
        """
        targets: list[tk.Widget] = [self]
        stack = list(self.winfo_children())
        while stack:
            widget = stack.pop()
            targets.append(widget)
            stack.extend(widget.winfo_children())
        for widget in targets:
            widget.bind("<Button-1>", self._pressed, add="+")
            widget.bind("<ButtonRelease-1>", self._released, add="+")
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")
            if not widget.cget("cursor"):
                widget["cursor"] = "hand2" if self._command else ""
        self._click_targets = targets

    def contains_pointer(self, event: tk.Event) -> bool:
        """"Soltou dentro" por retângulo na tela, não pela coordenada local do filho.

        A coordenada de `event.x` é relativa ao widget que recebeu o evento — um rótulo dentro da
        linha não diz nada sobre a linha. E `winfo_containing` depende de empilhamento, o que
        tornaria o teste de regressão sensível a janela sobreposta.
        """
        left, top = self.winfo_rootx(), self.winfo_rooty()
        return (left <= event.x_root < left + self.winfo_width()
                and top <= event.y_root < top + self.winfo_height())

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self.configure(cursor="hand2" if enabled and self._command else "")
        self.set_fill(self._idle_fill if enabled else theme.BG_CARD_SUBTLE)

    def set_fill_idle(self, color: str) -> None:
        self._idle_fill = color
        self.set_fill(color)

    def _pressed(self, _event: tk.Event) -> None:
        if self._enabled:
            self.set_fill(self._active_fill)

    def _released(self, event: tk.Event) -> None:
        if not self._enabled:
            return
        inside = self.contains_pointer(event)
        self.set_fill(self._hover_fill if inside else self._idle_fill)
        if inside and self._command:
            self._command()

    def _on_enter(self, _event: tk.Event) -> None:
        if self._enabled:
            self.set_fill(self._hover_fill)

    def _on_leave(self, _event: tk.Event) -> None:
        if self._enabled:
            self.set_fill(self._idle_fill)

    def _on_focus(self, _event: tk.Event) -> None:
        self.set_outline(theme.ACCENT, 2)

    def _on_blur(self, _event: tk.Event) -> None:
        self.set_outline(self._idle_outline, 1)

    def _activate(self, _event: tk.Event) -> None:
        if self._enabled and self._command:
            self._command()


class NavRow(Pressable):
    """Linha de navegação: glifo, rótulo e lista de seleção à esquerda quando ativa."""

    def __init__(self, master: tk.Misc, *, label: str, icon: str,
                 command: Callable[[], None]) -> None:
        super().__init__(master, fill=theme.BG_SIDEBAR, hover="#0B1826", active="#0E1E30",
                         radius=theme.RADIUS_CONTROL, command=command, name="memory_button" if icon == "memory" else f"nav_{icon}",
                         padding=(theme.SPACE_3, 0, theme.SPACE_3, 0))
        self.label_text, self.icon_name, self.selected = label, icon, False
        self._rail = False
        self._icon = IconCanvas(self.inner, icon, glyphs.NAV_ICON, theme.TEXT_MUTED,
                                theme.BG_SIDEBAR)
        self._icon.pack(side="left", padx=(theme.SPACE_2, theme.SPACE_3), pady=(0, 0))
        self._text = tk.Label(self.inner, text=label, font=theme.F_NAV,
                              background=theme.BG_SIDEBAR, foreground=theme.TEXT_SECONDARY)
        self._text.pack(side="left")
        # Altura em píxeis **físicos**: a opção `height` de um Canvas não é escalada pelo
        # `tk scaling`, mas a fonte dentro dele é. Fixado em 44, o rótulo da linha pediu 42
        # píxeis e recebeu 40 em 200 % — achado da varredura de escala, não previsão.
        self.configure(width=1, height=theme.to_physical(self, theme.ROW_HEIGHT))
        self.install_click()

    def set_rail(self, rail: bool) -> None:
        self._rail = rail
        if rail:
            self._text.pack_forget()
            self._icon.pack_forget()
            self._icon.pack(side="top", padx=0, pady=(10, 0))
        else:
            self._icon.pack_forget()
            self._text.pack_forget()
            self._icon.pack(side="left", padx=(theme.SPACE_2, theme.SPACE_3))
            self._text.pack(side="left")

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        fill = "#12233A" if selected else theme.BG_SIDEBAR
        tone = theme.ACCENT if selected else theme.TEXT_SECONDARY
        self.set_fill_idle(fill)
        self._text.configure(background=fill, foreground=tone,
                             font=theme.F_NAV_ACTIVE if selected else theme.F_NAV)
        self._icon.set_background(fill)
        self._icon.set_color(theme.ACCENT if selected else theme.TEXT_MUTED)
        # A lista de seleção é forma + cor + peso; nunca só uma delas.
        self._idle_outline = theme.BORDER_SUBTLE if selected else ""
        self.set_outline(self._idle_outline, 1)


class Pill(Pressable):
    """Botão-pílula de conteúdo real: glifo opcional, texto e chevron."""

    def __init__(self, master: tk.Misc, *, text: str, command: Callable[[], None],
                 icon: str = "", radius: int = theme.RADIUS_PILL, fill: str = theme.BG_CARD,
                 hover: str = "#16294A", name: str = "") -> None:
        super().__init__(master, fill=fill, hover=hover, active=fill, radius=radius,
                         command=command, name=name, fit=True,
                         padding=(theme.SPACE_4, theme.SPACE_2, theme.SPACE_3, theme.SPACE_2))
        self._icon = None
        if icon:
            self._icon = IconCanvas(self.inner, icon, glyphs.ICON_SIZE, theme.ACCENT, fill)
            self._icon.pack(side="left", padx=(0, theme.SPACE_2))
        self._text = tk.Label(self.inner, text=text, font=theme.F_BODY_MEDIUM,
                              background=fill, foreground=theme.TEXT_PRIMARY)
        self._text.pack(side="left")
        self._chevron = IconCanvas(self.inner, "chevron-down", 14, theme.ACCENT, fill)
        self._chevron.pack(side="left", padx=(theme.SPACE_2, 0))
        self.install_click()
        self.relayout()

    def set_text(self, text: str) -> None:
        self._text.configure(text=text)
        self.relayout()

    def get_text(self) -> str:
        """O que a pílula mostra agora — para teste, não para o usuário."""
        return str(self._text.cget("text"))


class Toggle(tk.Frame):
    """Interruptor desenhado: pista arredondada e rótulo ao lado, estado dito por palavra.

    O rótulo é irmão da pista, não filho dela. Enquanto morou dentro de uma superfície `fit`, o
    ajuste de largura media o texto um ciclo antes de ele crescer e "desligada" sangrava para fora
    da pílula — medido na captura de 13/10. Um quadro comum resolve sem disputa de geometria.
    """

    def __init__(self, master: tk.Misc, *, label: str, command: Callable[[bool], None],
                 name: str = "", background: str = theme.BG_SIDEBAR) -> None:
        super().__init__(master, background=background, name=name)
        self._value = False
        self._on_change = command
        self._base_label = label
        self._background = background
        self._enabled = True
        self._switch = tk.Canvas(self, width=48, height=26, background=background,
                                 highlightthickness=0, bd=0, cursor="hand2", takefocus=1,
                                 name="auto_approve_switch")
        self._switch.pack(side="left")
        self._label = tk.Label(self, text=label, font=theme.F_BODY, background=background,
                               foreground=theme.TEXT_PRIMARY)
        self._label.pack(side="left", padx=(theme.SPACE_2, 0))
        self._switch.bind("<Button-1>", lambda _e: self._clicked())
        self._switch.bind("<FocusIn>", self._on_focus)
        self._switch.bind("<FocusOut>", self._on_blur)
        self._switch.bind("<KeyRelease-Return>", lambda _e: self._clicked())
        self._switch.bind("<KeyRelease-space>", lambda _e: self._clicked())
        self._switch.bind("<Key-Return>", lambda _e: "break")
        self._switch.bind("<Key-space>", lambda _e: "break")
        self._repaint_knob()

    def _clicked(self) -> None:
        if not self._enabled:
            return
        self._value = not self._value
        self._repaint_knob()
        self._on_change(self._value)

    def _on_focus(self, _event: tk.Event) -> None:
        self._switch.configure(highlightbackground=theme.ACCENT, highlightcolor=theme.ACCENT)
        self._repaint_knob()

    def _on_blur(self, _event: tk.Event) -> None:
        self._switch.configure(highlightbackground=self._background,
                               highlightcolor=self._background)
        self._repaint_knob()

    def set_value(self, value: bool) -> None:
        self._value = bool(value)
        self._repaint_knob()

    def get_value(self) -> bool:
        return self._value

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._switch.configure(cursor="hand2" if enabled else "", takefocus=1 if enabled else 0)
        self._label.configure(foreground=theme.TEXT_PRIMARY if enabled else theme.DISABLED)
        self._repaint_knob()

    def _repaint_knob(self) -> None:
        track = self._switch
        track.delete("all")
        focused = track.focus_get() is track
        ring = theme.ACCENT if focused else self._background
        track.create_polygon(theme.round_rect_points(3, 2, 45, 24, 11), smooth=True,
                             splinesteps=14,
                             fill=theme.ACCENT if self._value else "#22374F",
                             outline=ring, width=2 if focused else 0)
        radius = 8.0
        center = 34.0 if self._value else 14.0
        tone = theme.TEXT_PRIMARY if self._value else theme.TEXT_MUTED
        track.create_oval(center - radius, 13 - radius, center + radius, 13 + radius,
                          fill=tone, outline=tone, width=0)
        # O estado é dito por palavra, não só por cor e posição do botão.
        self._label.configure(text=f"{self._base_label}  ·  "
                                   f"{'ligada' if self._value else 'desligada'}")


class PanelCard(theme.Surface):
    """Cartão do painel direito: cabeçalho com glifo, título, link e corpo.

    `fit="height"`: a altura é o conteúdo, a largura é a coluna. Com a superfície em extensão
    livre, o cartão ficava com altura de `Canvas` e o quarto botão de ação rápida existia mapeado
    **fora da área desenhada** — rolagem externa nenhuma recupera filho cortado dentro do cartão
    (visto na captura de 1280 em 13/10 19:03).
    """

    def __init__(self, master: tk.Misc, *, title: str, icon: str, name: str,
                 link_text: str = "", on_link: Callable[[], None] | None = None) -> None:
        super().__init__(master, fill=theme.BG_CARD_SUBTLE, outline=theme.BORDER_SUBTLE,
                         radius=theme.RADIUS_PANEL, fit="height",
                         padding=(theme.SPACE_4, theme.SPACE_4, theme.SPACE_4, theme.SPACE_4),
                         name=name)
        header = tk.Frame(self.inner, background=theme.BG_CARD_SUBTLE)
        IconCanvas(header, icon, glyphs.LIST_ICON, theme.ACCENT,
                   theme.BG_CARD_SUBTLE).pack(side="left", padx=(0, theme.SPACE_2))
        tk.Label(header, text=title, font=theme.F_SECTION, background=theme.BG_CARD_SUBTLE,
                 foreground=theme.TEXT_PRIMARY).pack(side="left")
        self.link = None
        if link_text and on_link:
            self.link = tk.Label(header, text=link_text, font=theme.F_SMALL, cursor="hand2",
                                 background=theme.BG_CARD_SUBTLE, foreground=theme.ACCENT)
            self.link.pack(side="right")
            self.link.bind("<Button-1>", lambda _e: on_link())
        header.pack(fill="x")
        self.body = tk.Frame(self.inner, background=theme.BG_CARD_SUBTLE)
        self.body.pack(fill="both", expand=True, pady=(theme.SPACE_3, 0))


class BrandHeader(tk.Frame):
    """Marca: meteoros desenhados + L U N M I. O espaçamento vem do texto, não de tracking."""

    def __init__(self, master: tk.Misc, *, on_click: Callable[[], None] | None = None,
                 show_word: bool = True) -> None:
        super().__init__(master, background=theme.BG_SIDEBAR)
        self.canvas = tk.Canvas(self, width=34, height=34, background=theme.BG_SIDEBAR,
                                highlightthickness=0, bd=0)
        self.canvas.pack(side="left")
        glyphs.meteor_mark(self.canvas, 17, 17, 30, head=theme.METEOR_HEAD,
                           tail=theme.METEOR_TAIL)
        self.word = tk.Label(self, text="L U N M I", font=theme.F_BRAND,
                             background=theme.BG_SIDEBAR, foreground=theme.TEXT_PRIMARY)
        if show_word:
            self.word.pack(side="left", padx=(theme.SPACE_3, 0))
        if on_click:
            for widget in (self, self.canvas, self.word):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda _e: on_click())

    def set_word_visible(self, visible: bool) -> None:
        if visible and not self.word.winfo_ismapped():
            self.word.pack(side="left", padx=(theme.SPACE_3, 0))
        elif not visible:
            self.word.pack_forget()


class BrandFooter(tk.Frame):
    """Identidade discreta no pé da barra: dois rastros longos e a frase do produto."""

    PHRASE = "Seu assistente\npara um dia\nmais fluido."

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, background=theme.BG_SIDEBAR)
        self.art = tk.Canvas(self, height=96, background=theme.BG_SIDEBAR,
                             highlightthickness=0, bd=0)
        self.art.pack(fill="x")
        self.art.bind("<Configure>", self._repaint)
        tk.Label(self, text="L U N M I", font=theme.F_SECTION, background=theme.BG_SIDEBAR,
                 foreground=theme.TEXT_SECONDARY).pack(anchor="w")
        tk.Label(self, text=self.PHRASE, font=theme.F_SMALL, justify="left",
                 background=theme.BG_SIDEBAR, foreground=theme.TEXT_MUTED).pack(anchor="w",
                                                                                pady=(2, 0))

    def _repaint(self, event: tk.Event) -> None:
        self.art.delete("all")
        width = event.width
        glyphs.meteor_streak(self.art, width * 0.06, 74, width * 0.42, 16,
                             tail=theme.METEOR_TAIL, head=theme.METEOR_HEAD)
        glyphs.meteor_streak(self.art, width * 0.30, 88, width * 0.62, 40,
                             tail="#33507A", head=theme.METEOR_HEAD)
        glyphs.starfield(self.art, width, 96, color=theme.STAR, count=9, seed=11)


GAP = theme.SPACE_4
TSide = Literal["left", "right", "top", "bottom"]
_W = TypeVar("_W", bound=tk.Widget)


class Shell(tk.Frame):
    """Casca de três colunas. Decide em pixel lógico, desenha em físico.

    O fundo é um `Canvas` com o céu estático redesenhado por evento de tamanho — não um timer. As
    três regiões usam `place` com larguras derivadas do tamanho real da janela.
    """

    def __init__(self, master: tk.Misc, *, on_navigate: Callable[[str], None]) -> None:
        super().__init__(master, background=theme.BG_WINDOW, name="shell")
        self._on_navigate = on_navigate
        self.background = tk.Canvas(self, background=theme.BG_WINDOW, highlightthickness=0, bd=0)
        self.background.place(x=0, y=0, relwidth=1, relheight=1)
        self.sidebar = tk.Frame(self, background=theme.BG_SIDEBAR, name="sidebar")
        self.main = tk.Frame(self, background=theme.BG_CHAT, name="main")
        self.right = tk.Frame(self, background=theme.BG_WINDOW, name="right_panel")
        self._rows: dict[str, NavRow] = {}
        self._active = ""
        self._rail = False
        self._right_open = False
        self._screen_mode = False
        self._panels_toggle: Pressable | None = None
        self._relayout_job: str | None = None

        self.brand = BrandHeader(self.sidebar, on_click=lambda: self._on_navigate("chat"))
        self.brand.pack(fill="x", padx=theme.SPACE_4, pady=(theme.SPACE_5, theme.SPACE_5))
        self.nav = tk.Frame(self.sidebar, background=theme.BG_SIDEBAR)
        self.nav.pack(fill="x", padx=theme.SPACE_3)
        self.footer = BrandFooter(self.sidebar)

        self.topbar = tk.Frame(self.main, background=theme.BG_CHAT, name="topbar")
        self.topbar.pack(fill="x")
        self.topbar_rows = [tk.Frame(self.topbar, background=theme.BG_CHAT),
                            tk.Frame(self.topbar, background=theme.BG_CHAT)]
        for row in self.topbar_rows:
            row.pack(fill="x", pady=(0, theme.SPACE_3))
        self.content = tk.Frame(self.main, background=theme.BG_CHAT, name="content")
        self.content.pack(fill="both", expand=True)
        self.dock = tk.Frame(self.main, background=theme.BG_CHAT, name="dock")
        self.dock.pack(fill="x")

        self.bind("<Configure>", self._on_configure)

    # -------------------------------------------------------------------- peças --
    def add_route(self, key: str, label: str, icon: str, *, enabled: bool = True) -> NavRow:
        def go(name: str = key) -> None:
            self._on_navigate(name)

        row = NavRow(self.nav, label=label, icon=icon, command=go)
        row.pack(fill="x", pady=2)
        if not enabled:
            row.set_enabled(False)
        self._rows[key] = row
        return row

    def set_active(self, key: str) -> None:
        self._active = key
        for name, row in self._rows.items():
            row.set_selected(name == key)

    def set_panels_toggle(self, command: Callable[[], None]) -> Pressable:
        """Botão que abre o painel direito nas janelas onde ele não cabe fixo.

        Vai pelo mesmo `pack` das outras linhas, não por `place` sobre uma superfície `fit`:
        `place` + auto-tamanho a partir do filho formam um ciclo — a pílula media o rótulo antes
        de o rótulo saber o próprio tamanho, e ele saía com 36 dos 68 px pedidos.
        """
        pill = Pill(self.topbar_rows[0], text="Painéis", command=command, icon="bolt",
                    name="panels_toggle")
        self._panels_toggle = pill
        return pill

    def set_screen_mode(self, enabled: bool) -> None:
        self._screen_mode = enabled
        self._run_relayout()

    def toggle_right(self) -> bool:
        self._right_open = not self._right_open
        self._run_relayout()
        return self._right_open

    # ------------------------------------------------------------------ layout --
    def _on_configure(self, _event: tk.Event) -> None:
        if self._relayout_job is not None:
            return
        self._relayout_job = self.after(16, self._run_relayout)

    def _run_relayout(self) -> None:
        self._relayout_job = None
        width, height = self.winfo_width(), self.winfo_height()
        if width <= 1 or height <= 1:
            return
        scale = theme.display_scale(self)
        logical = width / scale
        rail = logical < theme.BREAK_MEDIUM
        wide = logical >= theme.BREAK_WIDE
        sidebar_logical = theme.SIDEBAR_RAIL if rail else (
            theme.SIDEBAR_WIDTH if wide else theme.SIDEBAR_COMPACT)
        sidebar = int(round(sidebar_logical * scale))
        gap = int(round(GAP * scale))

        self.background.configure(width=width, height=height)
        glyphs.starfield(self.background, width, height, color=theme.STAR, count=26, seed=7)
        tk.Widget.lower(self.background)

        self.sidebar.place(x=0, y=0, width=sidebar, height=height)
        if rail != self._rail:
            self.brand.set_word_visible(not rail)
            for row in self._rows.values():
                row.set_rail(rail)
            self._rail = rail

        # Rodapé de marca some em janela baixa (identidade não come altura de conversa) **e no
        # rail**: no rail de 68 px os dois rótulos pedem 68 e 79 e têm 36 — medido em 850x700 e
        # 650x620, eram os últimos cortes da varredura. A marca já está no cabeçalho quando o
        # rail não é o modo, e no rail o glifo do meteoro continua no topo da barra.
        show_footer = height >= int(round(620 * scale)) and not rail
        if show_footer and not self.footer.winfo_ismapped():
            self.footer.pack(side="bottom", fill="x", padx=theme.SPACE_4,
                             pady=(0, theme.SPACE_5), before=self.nav)
        elif not show_footer and self.footer.winfo_ismapped():
            self.footer.pack_forget()

        # Painel direito: fixo na janela larga, sobreposto por botão nas demais. Esconder é
        # `place_forget`, nunca destruir — o conteúdo e suas respostas assíncronas vivem adiante.
        if self._panels_toggle is not None:
            if wide or self._screen_mode:
                self._panels_toggle.pack_forget()
            elif not self._panels_toggle.winfo_ismapped():
                self._panels_toggle.pack(in_=self.topbar_rows[0], side="right",
                                         padx=theme.SPACE_2)
        if not self._screen_mode and (wide or self._right_open):
            panel = min(int(round(theme.PANEL_WIDTH * scale)),
                        max(140, width - sidebar - 2 * gap))
            self.right.place(x=width - panel - gap, y=gap, width=panel, height=height - 2 * gap)
            self.right.lift()
            main_width = width - sidebar - panel - 2 * gap
        else:
            self.right.place_forget()
            main_width = width - sidebar - gap
        self.main.place(x=sidebar + gap, y=gap, width=max(80, main_width),
                        height=height - 2 * gap)

    def add_topbar(self, widget: _W, *, secondary: bool = False,
                   side: TSide = "left", padx: int = theme.SPACE_3) -> _W:
        """Item da barra de topo. `secondary` vai para a segunda linha, sempre.

        Duas linhas fixas, decididas por papel e não por medição. A versão anterior tentava
        decidir em tempo de layout quantas linhas cabiam, e criou três defeitos de uma vez: o
        primeiro item adicionado antes do primeiro `<Configure>` nunca era empacotado (a pílula
        ficava não-mapeada, 1x1, sem clique possível), e a comparação pelo número de linhas
        escondia isso. Medido em 680 px, com tudo numa linha, a barra pedia 998 px e o rótulo
        "Conversar (não executa ações)" ficava com 91 dos 200 px que pede.

        O custo é altura fixa na barra; o ganho é o modo nunca poder ser cortado — que é o
        requisito. Layout que depende do que o widget ainda não mediu não é layout, é corrida.
        """
        row = self.topbar_rows[1 if secondary else 0]
        widget.pack(in_=row, side=side, padx=padx)
        return widget

    def destroy(self) -> None:
        if self._relayout_job is not None:
            try:
                self.after_cancel(self._relayout_job)
            except tk.TclError:
                pass
            self._relayout_job = None
        super().destroy()
