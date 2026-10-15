"""Tokens, contraste e primitivas de superfície da interface Lunmi.

Tk 8.6 não tem raio em widget, nem opacidade por item, nem fundo translúcido: o `vista` nativo
ainda por cima ignora `background`/`foreground` de `TButton` (medido em captura, 12/10). Por isso
este módulo faz três coisas e só:

- fixa a paleta e a tipografia com o contraste calculado, não escolhido de ouvido;
- dá a primitiva `Surface`, que desenha canto arredondado de verdade num `Canvas` e hospeda por
  cima o widget nativo — seleção, foco e teclado continuam sendo do widget;
- configura o `ttk` em `clam`, o único tema que obedece cor, para que diálogos e barra de rolagem
  não fiquem claros num aplicativo escuro. A moldura da janela, o arraste, o DPI e os diálogos de
  arquivo permanecem nativos do Windows.
"""
from __future__ import annotations

import tkinter as tk
import math
import struct
import zlib
from collections.abc import Callable
from tkinter import font as tkfont
from tkinter import ttk
from typing import Literal

# ------------------------------------------------------------------------------------ paleta --
BG_WINDOW = "#06101E"        # fundo da janela
BG_SIDEBAR = "#050D1A"       # barra lateral esquerda
BG_CHAT = "#071323"          # área do chat
BG_CARD = "#101E30"          # resposta da assistente e cartões de destaque
BG_CARD_SUBTLE = "#0B192B"   # painel direito e listas
BG_USER = "#182A43"          # bolha do usuário
BG_INPUT = "#0C1A2E"         # campo de texto
BORDER_SUBTLE = "#24364F"    # contorno de 1 px
BORDER_HAIRLINE = "#182A42"  # divisor interno, mais quieto que o contorno
# Contorno da bolha do usuário: um azul acima do fundo dela. O valor antigo (#C3DBD6) era da
# paleta clara e ficou visível como filete claro em tela escura na primeira captura da Lunmi.
BORDER_ON_USER = "#2C4568"
TEXT_PRIMARY = "#F3F6FC"
TEXT_SECONDARY = "#B4C3D7"
TEXT_MUTED = "#91A3BD"
# A paleta da especificação usa o mesmo muted em toda parte. Sobre `bg.user` ele dá 4,492:1 e
# reprovaria texto pequeno; aqui o secundário é tingido do azul da própria superfície, como manda
# a regra de superfícies coloridas — nunca cinza sobre cor.
TEXT_MUTED_ON_USER = "#8FA3C0"
ACCENT = "#7DAFFF"
ACCENT_HOVER = "#95BFFF"
ACCENT_PRESSED = "#6B99E6"
ON_ACCENT = "#06101E"        # seta e rótulo sobre o azul: 8,584:1
SUCCESS = "#76CFA9"
WARNING = "#E7BB75"
ERROR = "#E58D99"
# Desativado não é texto legível, é estado — mas estado que se lê. O WCAG isenta componente
# inativo; esta interface não, porque um rótulo apagado demais vira ruído em tela escura.
# #4A5C75 dava 2,591:1 sobre o painel sutil. Este tom fica 4,011:1 e continua abaixo do discreto.
DISABLED = "#647A96"
STAR = "#22364F"             # ponto estelar do fundo estático
METEOR_HEAD = "#C9DCFF"
METEOR_TAIL = "#4A6B93"
SURFACE_HOVER = "#162A45"    # linha de painel sob o ponteiro

# ---------------------------------------------------------------------------------- contraste --
def channel_linear(value: int) -> float:
    return value / 255 / 12.92 if value <= 10 else ((value / 255 + 0.055) / 1.055) ** 2.4


def normalize_hex(color: str) -> str:
    """Cor comparável: o Tk devolve `#rrggbb` minúsculo e a paleta daqui é maiúscula."""
    return color.upper()


def luminance(color: str) -> float:
    hex_digits = color.lstrip("#")
    red, green, blue = (int(hex_digits[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * channel_linear(red) + 0.7152 * channel_linear(green)
            + 0.0722 * channel_linear(blue))


def contrast(first: str, second: str) -> float:
    """Razão WCAG 2.1 entre duas cores opacas."""
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# Cada par que a interface jura, com o piso dele escrito ao lado — não inferido do rótulo. Texto
# normal exige 4,5:1; texto grande, ícone essencial e estado de controle exigem 3:1; divisor
# decorativo não herda requisito nenhum (análise §8). `tests/test_lunmi_ui.py` percorre a lista.
TEXT_FLOOR = 4.5           # texto normal pequeno
LARGE_TEXT_FLOOR = 3.0     # texto grande e rótulo de controle
GRAPHIC_FLOOR = 3.0        # ícone essencial e estado de controle
DECORATIVE_FLOOR = 0.0     # não é informação; existe para separar camadas

CONTRAST_PAIRS: tuple[tuple[str, str, str, str, float], ...] = (
    ("texto principal", "janela", TEXT_PRIMARY, BG_WINDOW, TEXT_FLOOR),
    ("texto principal", "chat", TEXT_PRIMARY, BG_CHAT, TEXT_FLOOR),
    ("texto principal", "cartão", TEXT_PRIMARY, BG_CARD, TEXT_FLOOR),
    ("texto principal", "bolha do usuário", TEXT_PRIMARY, BG_USER, TEXT_FLOOR),
    ("texto secundário", "cartão", TEXT_SECONDARY, BG_CARD, TEXT_FLOOR),
    ("texto secundário", "sidebar", TEXT_SECONDARY, BG_SIDEBAR, TEXT_FLOOR),
    ("texto discreto", "janela", TEXT_MUTED, BG_WINDOW, TEXT_FLOOR),
    ("texto discreto", "cartão", TEXT_MUTED, BG_CARD, TEXT_FLOOR),
    ("texto discreto", "painel sutil", TEXT_MUTED, BG_CARD_SUBTLE, TEXT_FLOOR),
    ("texto discreto", "bolha do usuário", TEXT_MUTED_ON_USER, BG_USER, TEXT_FLOOR),
    ("horário na bolha", "bolha do usuário", TEXT_MUTED_ON_USER, BG_USER, TEXT_FLOOR),
    # U2: as três tintas que o transcurso e o compositor estrearam entram no juramento junto.
    ("narração do sistema", "chat", TEXT_MUTED, BG_CHAT, TEXT_FLOOR),
    ("placeholder do campo", "campo de escrita", TEXT_MUTED, BG_INPUT, TEXT_FLOOR),
    ("texto no campo", "campo de escrita", TEXT_PRIMARY, BG_INPUT, TEXT_FLOOR),
    ("texto selecionado", "marcação do campo", ON_ACCENT, ACCENT, TEXT_FLOOR),
    # U4: as superfícies do painel direito entram no juramento com o resto.
    ("título do cartão", "painel sutil", TEXT_PRIMARY, BG_CARD_SUBTLE, TEXT_FLOOR),
    ("linha de tarefa recente", "painel sutil", TEXT_SECONDARY, BG_CARD_SUBTLE, TEXT_FLOOR),
    ("nota e estado da memória", "painel sutil", TEXT_MUTED, BG_CARD_SUBTLE, TEXT_FLOOR),
    ("link gerenciar", "painel sutil", ACCENT, BG_CARD_SUBTLE, TEXT_FLOOR),

    ("rótulo da rota ativa", "sidebar", ACCENT, BG_SIDEBAR, TEXT_FLOOR),
    ("marcador do Brain", "cartão", ACCENT, BG_CARD, LARGE_TEXT_FLOOR),
    ("seta de enviar", "botão azul", ON_ACCENT, ACCENT, GRAPHIC_FLOOR),
    ("sucesso", "cartão", SUCCESS, BG_CARD, TEXT_FLOOR),
    ("atenção", "cartão", WARNING, BG_CARD, TEXT_FLOOR),
    ("falha", "cartão", ERROR, BG_CARD, TEXT_FLOOR),
    ("texto desativado", "painel sutil", DISABLED, BG_CARD_SUBTLE, LARGE_TEXT_FLOOR),
    ("divisor de seção", "janela", BORDER_SUBTLE, BG_WINDOW, DECORATIVE_FLOOR),
)


def failing_pairs() -> list[str]:
    """Pares abaixo do próprio piso. Vazio é condição de entrega."""
    offenders = []
    for label, surface, foreground, background, floor in CONTRAST_PAIRS:
        if floor <= 0:
            continue
        ratio = contrast(foreground, background)
        if ratio < floor:
            offenders.append(f"{label} sobre {surface}: {ratio:.3f}:1 < {floor}:1")
    return offenders


# --------------------------------------------------------------------------------- tipografia --
# As quatro faces Variable abaixo foram medidas nesta máquina: cada uma resolve num arquivo
# próprio e com largura própria (111 / 115 / 108 / 110 px para a mesma amostra em 10 pt). Não há
# substituição disfarçada nem fonte baixada.
FAMILY_TEXT = "Segoe UI Variable Text"
FAMILY_TEXT_SEMIBOLD = "Segoe UI Variable Text Semibold"
FAMILY_DISPLAY = "Segoe UI Variable Display"
FAMILY_DISPLAY_SEMIBOLD = "Segoe UI Variable Display Semib"
FAMILY_DATA = "Cascadia Mono"

# Tk mede fonte em pontos, e pontos escalam com o DPI da sessão — é assim que 125/150/200% continuam
# legíveis sem coordenada fixa. A referência especifica em pixels a 96 dpi: 1 px = 0,75 pt.
def px_to_pt(px: float) -> int:
    return max(7, int(round(px * 0.75)))


BODY_PT = 11
SECTION_PT = px_to_pt(15)     # 11 pt
SMALL_PT = px_to_pt(12)       # 9 pt
BRAND_PT = px_to_pt(17)       # 13 pt

F_BODY = (FAMILY_TEXT, BODY_PT)
F_BODY_MEDIUM = (FAMILY_TEXT_SEMIBOLD, BODY_PT)
F_SECTION = (FAMILY_DISPLAY_SEMIBOLD, SECTION_PT)
F_SMALL = (FAMILY_TEXT, SMALL_PT)
F_TIME = (FAMILY_TEXT, px_to_pt(11))
F_NAV = (FAMILY_TEXT, BODY_PT)
F_NAV_ACTIVE = (FAMILY_TEXT_SEMIBOLD, BODY_PT)
F_BRAND = (FAMILY_DISPLAY_SEMIBOLD, BRAND_PT)
F_DATA = (FAMILY_DATA, SMALL_PT)

# ----------------------------------------------------------------------- escala e geometria --
SPACE_1, SPACE_2, SPACE_3, SPACE_4, SPACE_5, SPACE_6 = 4, 8, 12, 16, 20, 24
RADIUS_PANEL = 16        # painéis grandes: 14–18 px
RADIUS_CONTROL = 12      # botões e linhas do painel: 10–12 px
RADIUS_PILL = 999        # pills e botões circulares: raio completo
ROW_HEIGHT = 44          # linha clicável: 40–46 px
SIDEBAR_WIDTH = 216
SIDEBAR_RAIL = 68        # trilho só de ícones na janela estreita
PANEL_WIDTH = 292
# A spec estima "alvo aproximado 680 × 560" e manda definir o mínimo por medida. Medido com o
# cabeçalho em duas linhas: em 680 a linha do topo pede 609 px para 596 disponíveis (corta
# Memória/Configurar); em 700 já cabe. O mínimo declarado é o que a interface sustenta, não o que
# o mockup sugeriu — e a diferença fica escrita aqui em vez de virar corte na tela.
MIN_WIDTH, MIN_HEIGHT = 700, 560
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1280, 800

# Pixels da especificação são pixels a 100%. Comparar largura física de janela contra um número de
# design faria o breakpoint mudar de lugar em 150% — o layout decidiria em pixels lógicos e só
# desenharia em físicos.
BREAK_WIDE = 1180          # três colunas visíveis
BREAK_MEDIUM = 850         # painel direito atrás de botão; sidebar compacta
BREAK_RAIL = 650           # sidebar vira trilho de ícones
SIDEBAR_COMPACT = 196


def display_scale(widget: tk.Misc) -> float:
    """Pixels por polegada do Tk desta tela, sobre 96: 1,0 a 100%, 1,5 a 150%."""
    try:
        return max(1.0, widget.winfo_fpixels("1i") / 96.0)
    except tk.TclError:
        return 1.0


def to_physical(widget: tk.Misc, logical: float) -> int:
    return int(round(logical * display_scale(widget)))


def logical_width(widget: tk.Misc) -> float:
    try:
        return widget.winfo_width() / display_scale(widget)
    except tk.TclError:
        return 0.0

# --------------------------------------------------------------------------- primitivas --
_SQUARE = (0.0, 0.0, 0.0, 0.0)


def round_rect_points(x0: float, y0: float, x1: float, y1: float, radius: float) -> list[float]:
    """Pontos de um retângulo arredondado para `create_polygon(smooth=True)`.

    A spline passa tangente nas arestas e fecha os cantos; duplicar os pontos de cada canto é o
    que segura a rectilinearidade das laterais. Não é aproximação de círculo: é a forma.
    """
    r = max(0.0, min(radius, (y1 - y0) / 2, (x1 - x0) / 2))
    return [
        x0 + r, y0, x0 + r, y0,
        x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r,
        x1, y1 - r, x1, y1 - r, x1, y1, x1 - r, y1, x1 - r, y1,
        x0 + r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y1 - r,
        x0, y0 + r, x0, y0 + r, x0, y0,
    ]


class Surface(tk.Canvas):
    """Superfície de cantos arredondados com um contêiner interno.

    A superfície desenha só o fundo arredondado e hospeda **um** frame (`inner`). O conteúdo
    dentro dele é gerenciado pelo `pack`/`grid` do Tk, não por esta classe. Tentar ser dois
    gerenciadores de geometria ao mesmo tempo — como esta classe fazia antes — põe dois donos na
    mesma criança e o resultado é texto sobreposto a ícone e título invisível sob o corpo.

    Dois modos. `stretch` (padrão) ocupa o que o gerenciador der. `fit` pede o tamanho do
    conteúdo — bolha de mensagem, pill, botão desenhado. `"height"`/`"width"` ajustam um eixo só:
    o compositor de mensagem cresce com o texto, mas a largura é a linha inteira. Medido em
    700x560 com `fit=True` nos dois eixos: o `pack` alocava 457 px, a superfície desenhava 280, e
    botão e campo ficavam para fora da caixa pintada.
    """

    def __init__(self, master: tk.Misc, *, fill: str, outline: str = "", border: int = 1,
                 radius: int = RADIUS_PANEL, background: str = BG_WINDOW,
                 fit: bool | Literal["height", "width"] = False,
                 padding: tuple[int, ...] = (0, 0, 0, 0), inner: tk.Widget | None = None,
                 inner_factory: Callable[[tk.Widget], tk.Widget] | None = None,
                 name: str = "") -> None:
        super().__init__(master, bg=background, highlightthickness=0, bd=0, width=1, height=1, name=name)
        self._fill, self._outline, self._border = fill, outline, border
        self._radius, self._fit = radius, fit
        # `padding` aceita (h, v) ou (esq, topo, dir, base) e vira sempre quatro.
        given = tuple(int(value) for value in padding)
        if len(given) == 2:
            given = (given[0], given[1], given[0], given[1])
        if len(given) != 4:
            raise ValueError(f"padding precisa de 2 ou 4 valores, recebido {padding!r}")
        self._padding: tuple[int, int, int, int] = given
        self._box = self.create_polygon(list(_SQUARE), smooth=True, splinesteps=14,
                                        fill=fill, outline=outline, width=border)
        # `inner` pode ser o próprio widget de conteúdo (um Text de bolha). Quando vem, a
        # superfície é só o fundo arredondado dele; sem ele, um frame neutro.
        # `inner_factory` existe por uma razão dura: o pai do widget tem de ser **este** Canvas,
        # e o Canvas só existe depois de `super().__init__`. Construir o Text do lado de fora com
        # `tk.Text(None, ...)` criaria uma janela de topo, não uma criança da bolha.
        if inner is not None:
            self.inner: tk.Widget = inner
        elif inner_factory is not None:
            self.inner = inner_factory(self)
        else:
            self.inner = tk.Frame(self, background=fill)
        self._slot = self.create_window(0, 0, anchor="nw", window=self.inner)
        self.bind("<Configure>", self._relayout)

    def inner_box(self) -> tuple[float, float, float, float]:
        left, top, right, bottom = self._padding
        width, height = self.winfo_width(), self.winfo_height()
        return (left + self._border, top + self._border,
                width - right - self._border, height - bottom - self._border)

    def relayout(self) -> None:
        self._relayout()

    def drawn_box(self) -> tuple[float, float, float, float]:
        """Caixa do polígono desenhado, em coordenadas do próprio widget.

        É o que a medição de interface precisa comparar com a coluna: a área pintada, não a
        margem pedida ao gerenciador. `bbox` de item devolve vazio antes do primeiro layout.
        """
        return self.bbox(self._box) or (0.0, 0.0, 0.0, 0.0)

    def outline_color(self) -> str:
        """Cor da borda desenhada — o que a interface afirma, lido do item, não de um cache."""
        return self.itemcget(self._box, "outline")

    def _relayout(self, *_: object) -> None:
        width, height = self.winfo_width(), self.winfo_height()
        if self._fit:
            try:
                # Do not re-enter layout here: child Configure handlers can resize this surface.
                wanted = (self.inner.winfo_reqwidth() + self._padding[0] + self._padding[2]
                          + 2 * self._border,
                          self.inner.winfo_reqheight() + self._padding[1] + self._padding[3]
                          + 2 * self._border)
            except tk.TclError:
                return
            hug_width = self._fit is True or self._fit == "width"
            hug_height = self._fit is True or self._fit == "height"
            size = (wanted[0] if hug_width else width, wanted[1] if hug_height else height)
            # A stretched axis must not inherit a previous allocation as its minimum.
            if hug_width and int(self.cget("width")) != wanted[0]:
                self.configure(width=wanted[0])
            if hug_height and int(self.cget("height")) != wanted[1]:
                self.configure(height=wanted[1])
            width, height = size
        if width <= 1:
            return
        pad_left, pad_top, pad_right, pad_bottom = self._padding
        left, top = pad_left + self._border, pad_top + self._border
        right, bottom = width - pad_right - self._border, height - pad_bottom - self._border
        self.coords(self._box, round_rect_points(self._border / 2, self._border / 2,
                                                 max(self._border, width - 1 - self._border / 2),
                                                 max(self._border, height - 1 - self._border / 2),
                                                 self._radius))
        self.coords(self._slot, left, top)
        self.itemconfigure(self._slot, anchor="nw",
                           width=max(1, int(right - left)), height=max(1, int(bottom - top)))

    def set_fill(self, color: str) -> None:
        self._fill = color
        self.itemconfigure(self._box, fill=color)
        try:
            # Assign por item, não por palavra-chave: `configure(bg=)` funciona em Frame e Text,
            # mas o tipado genérico de Widget não conhece a palavra — e `inner` é um Widget.
            self.inner["bg"] = color
        except tk.TclError:
            pass

    def set_outline(self, color: str, border: int | None = None) -> None:
        self._outline = color
        if border is not None:
            self._border = border
        self.itemconfigure(self._box, outline=color, width=self._border)
        self.relayout()


def apply(root: tk.Misc) -> ttk.Style:
    """Estilos ttk do mundo Lunmi. `clam` é o único tema que obedece background/foreground."""
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure(".", background=BG_WINDOW, foreground=TEXT_PRIMARY, fieldbackground=BG_INPUT,
                    bordercolor=BORDER_SUBTLE, lightcolor=BG_WINDOW, darkcolor=BG_WINDOW,
                    focuscolor=ACCENT, font=F_BODY)
    style.configure("TFrame", background=BG_WINDOW, borderwidth=0)
    style.configure("TLabel", background=BG_WINDOW, foreground=TEXT_PRIMARY, borderwidth=0,
                    font=F_BODY)
    style.configure("Muted.TLabel", background=BG_WINDOW, foreground=TEXT_MUTED, font=F_SMALL)
    style.configure("TCheckbutton", background=BG_WINDOW, foreground=TEXT_PRIMARY, font=F_BODY)
    style.map("TCheckbutton",
              background=[("active", BG_WINDOW), ("disabled", BG_WINDOW)],
              foreground=[("disabled", DISABLED)],
              indicatorcolor=[("selected", ACCENT)])
    style.configure("TEntry", fieldbackground=BG_INPUT, foreground=TEXT_PRIMARY,
                    insertcolor=ACCENT, bordercolor=BORDER_SUBTLE, lightcolor=BG_INPUT,
                    darkcolor=BG_INPUT, padding=(SPACE_3, SPACE_2), relief="flat")
    style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", BG_INPUT)],
              darkcolor=[("focus", BG_INPUT)], fieldbackground=[("disabled", BG_CARD_SUBTLE)])
    style.configure("TCombobox", fieldbackground=BG_INPUT, background=BG_CARD,
                    foreground=TEXT_PRIMARY, arrowcolor=ACCENT, bordercolor=BORDER_SUBTLE,
                    lightcolor=BG_INPUT, darkcolor=BG_INPUT, padding=SPACE_2)
    style.map("TCombobox", fieldbackground=[("readonly", BG_INPUT)],
              foreground=[("readonly", TEXT_PRIMARY)], selectbackground=[("readonly", BG_INPUT)],
              bordercolor=[("focus", ACCENT)])
    style.configure("TButton", background=BG_CARD, foreground=TEXT_PRIMARY, bordercolor=BORDER_SUBTLE,
                    lightcolor=BG_CARD, darkcolor=BG_CARD, focuscolor=ACCENT, relief="flat",
                    padding=(SPACE_4, SPACE_2), font=F_BODY_MEDIUM)
    style.map("TButton",
              background=[("disabled", BG_CARD_SUBTLE), ("active", SURFACE_HOVER),
                          ("pressed", BG_CARD)],
              foreground=[("disabled", DISABLED)], bordercolor=[("focus", ACCENT)])
    style.configure("TNotebook", background=BG_WINDOW, borderwidth=0, tabposition="n")
    style.configure("TNotebook.Tab", background=BG_CARD_SUBTLE, foreground=TEXT_SECONDARY,
                    padding=(SPACE_4, SPACE_2), font=F_BODY_MEDIUM)
    style.map("TNotebook.Tab", background=[("selected", BG_CARD)],
              foreground=[("selected", TEXT_PRIMARY)])
    style.configure("TScrollbutton", background=BG_CARD, foreground=TEXT_SECONDARY,
                    bordercolor=BORDER_SUBTLE, arrowcolor=TEXT_SECONDARY)
    # A calha é resolvida no estilo BASE: configurar só "Vertical.TScrollbar" deixava o elemento
    # `Vertical.Scrollbar.trough` lendo `-troughcolor` de "TScrollbar" (medido: #bab5ab, e a barra
    # saía #dcdad5 clara na captura). Os dois nomes precisam do mesmo valor.
    style.configure("TScrollbar", background=BG_CARD, troughcolor=BG_WINDOW,
                    bordercolor=BG_WINDOW, lightcolor=BG_CARD, darkcolor=BG_CARD,
                    arrowcolor=TEXT_SECONDARY, gripcolor=BORDER_SUBTLE, arrowsize=11)
    style.map("TScrollbar", background=[("active", ACCENT), ("pressed", ACCENT_HOVER)],
              troughcolor=[("disabled", BG_WINDOW)])
    style.configure("Vertical.TScrollbar", background=BG_CARD, troughcolor=BG_WINDOW,
                    bordercolor=BG_WINDOW, arrowcolor=TEXT_SECONDARY, gripcolor=BORDER_SUBTLE)
    style.configure("Horizontal.TScrollbar", background=BG_CARD, troughcolor=BG_WINDOW,
                    bordercolor=BG_WINDOW, arrowcolor=TEXT_SECONDARY, gripcolor=BORDER_SUBTLE)
    style.configure("TLabelframe", background=BG_WINDOW, bordercolor=BORDER_SUBTLE)
    style.configure("TLabelframe.Label", background=BG_WINDOW, foreground=TEXT_SECONDARY,
                    font=F_BODY_MEDIUM)
    style.configure("TProgressbar", background=ACCENT, troughcolor=BG_CARD_SUBTLE,
                    bordercolor=BG_CARD_SUBTLE, lightcolor=ACCENT, darkcolor=ACCENT)
    style.configure("Toolbutton", background=BG_WINDOW, foreground=TEXT_SECONDARY, borderwidth=0,
                    relief="flat", padding=SPACE_2)
    style.map("Toolbutton", background=[("active", BG_CARD), ("selected", BG_CARD)],
              foreground=[("disabled", DISABLED)])
    # Botão primário: fundo de destaque com texto escuro (8,584:1). Sob `vista` isto seria
    # impossível — branco sobre branco, medido em 12/10 — e é uma das razões do `clam`.
    style.configure("Lunmi.Send.TButton", background=ACCENT, foreground=ON_ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=ACCENT, focuscolor=BG_WINDOW,
                    font=F_BODY_MEDIUM, padding=(SPACE_4, SPACE_2))
    style.map("Lunmi.Send.TButton",
              background=[("disabled", BG_CARD_SUBTLE), ("pressed", ACCENT_PRESSED),
                          ("active", ACCENT_HOVER)],
              foreground=[("disabled", DISABLED)],
              bordercolor=[("disabled", BORDER_SUBTLE)])
    _rounded_buttons(root, style)
    return style


def rounded_tile(fill: str, outline: str, radius: int = 12,
                 backdrop: str = BG_CHAT) -> bytes:
    """Opaque rounded tile; a wide center avoids hundreds of tiny ttk image copies."""
    edge = 2 * radius + 32
    rgb = tuple(int(fill[i:i + 2], 16) for i in (1, 3, 5))
    stroke = tuple(int(outline[i:i + 2], 16) for i in (1, 3, 5))
    ground = tuple(int(backdrop[i:i + 2], 16) for i in (1, 3, 5))
    raw = bytearray()
    center = edge / 2
    for y in range(edge):
        raw.append(0)
        for x in range(edge):
            dx = max(abs(x + .5 - center) - (center - radius), 0)
            dy = max(abs(y + .5 - center) - (center - radius), 0)
            distance = math.hypot(dx, dy) - radius
            alpha = max(0.0, min(1.0, .5 - distance))
            inside = max(0.0, min(1.0, -.5 - distance))
            mix = inside / alpha if alpha else 0
            # Opaque antialiasing avoids Tk's slow alpha compositing on stretched ttk tiles.
            raw.extend(round((a * mix + b * (1 - mix)) * alpha + c * (1 - alpha))
                       for a, b, c in zip(rgb, stroke, ground))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('!I', len(data)) + kind + data + struct.pack(
            '!I', zlib.crc32(kind + data) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('!2I5B', edge, edge, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw))) + chunk(b'IEND', b''))


def _rounded_buttons(root: tk.Misc, style: ttk.Style) -> None:
    radius = to_physical(root, 18)
    images: list[tk.PhotoImage] = []
    variants = {
        'Lunmi.Rounded': (BG_CHAT, SURFACE_HOVER, BG_USER, BG_CHAT),
        'Lunmi.Primary': (ACCENT, ACCENT_HOVER, ACCENT_PRESSED, ACCENT),
        'Lunmi.Quiet': (BG_CARD_SUBTLE, SURFACE_HOVER, BG_CARD, BG_CARD_SUBTLE),
    }
    for name, (idle, hover, pressed, border) in variants.items():
        if name not in style.element_names():
            pictures = [tk.PhotoImage(master=root, data=rounded_tile(fill, line, radius,
                                                                  BG_INPUT if name == "Lunmi.Primary"
                                                                  else BG_CARD_SUBTLE if name == "Lunmi.Quiet"
                                                                  else BG_CHAT),
                                     format='png')
                        for fill, line in ((idle, border), (hover, border), (pressed, border),
                                           (BG_CARD_SUBTLE, BORDER_HAIRLINE), (idle, ACCENT))]
            images.extend(pictures)
            style.element_create(name, 'image', pictures[0], ('disabled', pictures[3]),
                                 ('pressed', pictures[2]), ('focus', pictures[4]),
                                 ('active', pictures[1]), ('selected', pictures[2]), border=radius + 1,
                                 padding=0, width=2 * radius + 2, height=2 * radius + 2, sticky='nsew')
        layout = [(name, {'sticky': 'nsew', 'children': [
            ('Button.padding', {'sticky': 'nsew', 'children': [
                ('Button.label', {'sticky': 'nsew'})]})]})]
        target = {'Lunmi.Rounded': 'TButton', 'Lunmi.Primary': 'Lunmi.Send.TButton',
                  'Lunmi.Quiet': 'Lunmi.Action.TButton'}[name]
        style.layout(target, layout)
        style.configure(target, padding=(16, 10), borderwidth=0,
                        background=BG_INPUT if name == 'Lunmi.Primary' else (
                            BG_CARD_SUBTLE if name == 'Lunmi.Quiet' else BG_CHAT),
                        anchor='w' if name == 'Lunmi.Quiet' else 'center')
        backdrop = BG_INPUT if name == 'Lunmi.Primary' else (
            BG_CARD_SUBTLE if name == 'Lunmi.Quiet' else BG_CHAT)
        style.map(target, background=[('disabled', backdrop), ('active', backdrop),
                                      ('pressed', backdrop), ('!disabled', backdrop)])
    style.layout("Lunmi.Mode.TRadiobutton", style.layout("TButton"))
    style.configure("Lunmi.Mode.TRadiobutton", padding=(14, 7), background=BG_CHAT,
                    foreground=TEXT_SECONDARY, font=F_SMALL)
    style.map("Lunmi.Mode.TRadiobutton", foreground=[("selected", TEXT_PRIMARY),
                                                   ("disabled", TEXT_MUTED)])
    style.configure("Treeview", background=BG_CARD_SUBTLE, fieldbackground=BG_CARD_SUBTLE,
                    foreground=TEXT_PRIMARY, borderwidth=0, rowheight=to_physical(root, 32))
    style.configure("Treeview.Heading", background=BG_CHAT, foreground=TEXT_SECONDARY,
                    relief="flat", font=F_SMALL)
    style.map("Treeview", background=[("selected", BG_USER)],
              foreground=[("selected", TEXT_PRIMARY)])
    # Keep Tcl images alive for this interpreter, also when a test reapplies the theme.
    owner = root._root()  # type: ignore[attr-defined]  # images belong to the Tcl interpreter
    retained = getattr(owner, '_lunmi_button_images', [])
    owner._lunmi_button_images = retained + images  # type: ignore[attr-defined]


def measure_font(role: tuple[str, int]) -> tkfont.Font:
    """Fonte real do toolkit para medir pixels — o layout arredonda sobre medida, não sobre chute."""
    return tkfont.Font(family=role[0], size=role[1])
