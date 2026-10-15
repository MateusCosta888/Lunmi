"""Ícones e marca da Lunmi, desenhados em geometria do próprio Tk.

Por que desenhar e não usar fonte de glifos: uma família de ícones precisa de peso de traço
constante e de tamanho óptico controlado. `Segoe Fluent Icons` existe nesta máquina, mas é texto —
herda entrelinha, caixa-alta e subpixel do texto, e um ícone que se comporta como letra nunca casa
com superfícies desenhadas. Aqui cada glifo é linha, arco e círculo num `Canvas`, com o mesmo traço
em toda a família.

Nada neste módulo anima. Estrelas e meteoros são decoração estática, desenhada uma vez: sem timer,
sem partícula, sem custo contínuo de CPU.
"""
from __future__ import annotations

import math
import random
import tkinter as tk
from typing import Callable

# Traço em relação ao corpo do ícone: ~1,4 px num glifo de 20 px. O mesmo número em toda a família
# é o que faz um conjunto de ícones parecer desenhado por uma mão só.
STROKE_RATIO = 0.072
ICON_SIZE = 20
NAV_ICON = 18
LIST_ICON = 20


def stroke_for(size: int | float) -> float:
    return max(1.0, round(size * STROKE_RATIO, 2))


class _Pen:
    """Grade de desenho: o glifo é especificado num quadrado 20×20 e escalado ao tamanho real."""

    def __init__(self, canvas: tk.Canvas, cx: float, cy: float, size: int, color: str,
                 tag: str) -> None:
        self.canvas = canvas
        self.cx, self.cy = cx, cy
        self.s = size / 20.0
        self.color = color
        self.tag = tag
        self.w = stroke_for(size)
        self.left = cx - 10 * self.s
        self.top = cy - 10 * self.s

    def p(self, x: float, y: float) -> tuple[float, float]:
        return self.left + x * self.s, self.top + y * self.s

    def path(self, *points: tuple[float, float], close: bool = False) -> None:
        flat = [value for point in points for value in self.p(*point)]
        if close:
            flat.extend(flat[:2])
        self.canvas.create_line(*flat, fill=self.color, width=self.w, capstyle="round",
                                joinstyle="round", tags=self.tag)

    def circle(self, x: float, y: float, radius: float, *, filled: bool = True) -> None:
        px, py = self.p(x, y)
        r = radius * self.s
        box = (px - r, py - r, px + r, py + r)
        if filled:
            self.canvas.create_oval(*box, fill=self.color, outline=self.color, width=0,
                                    tags=self.tag)
        else:
            self.canvas.create_oval(*box, fill="", outline=self.color, width=self.w,
                                    tags=self.tag)

    def arc(self, center: tuple[float, float], radius: tuple[float, float], start: float,
            extent: float) -> None:
        cx, cy = self.p(*center)
        rx, ry = radius[0] * self.s, radius[1] * self.s
        self.canvas.create_arc(cx - rx, cy - ry, cx + rx, cy + ry, start=start, extent=extent,
                               style="arc", outline=self.color, width=self.w, tags=self.tag)

    def rect(self, x0: float, y0: float, x1: float, y1: float, *, filled: bool = True) -> None:
        a, b = self.p(x0, y0), self.p(x1, y1)
        self.canvas.create_rectangle(*a, *b, fill=self.color if filled else "",
                                     outline=self.color, width=0 if filled else self.w,
                                     tags=self.tag)


_GLYPHS: dict[str, Callable[[_Pen], None]] = {}


def _register(name: str) -> Callable[[Callable[[_Pen], None]], Callable[[_Pen], None]]:
    def decorator(function: Callable[[_Pen], None]) -> Callable[[_Pen], None]:
        _GLYPHS[name] = function
        return function
    return decorator


@_register("chat")
def _(pen: _Pen) -> None:
    pen.path((3, 5), (17, 5), (17, 14), (9, 14), (6, 17), (6, 14), (3, 14), close=True)


@_register("memory")
def _(pen: _Pen) -> None:
    pen.path((5, 3), (15, 3), (15, 17), (10, 13), (5, 17), close=True)


@_register("computer")
def _(pen: _Pen) -> None:
    pen.path((2.5, 4), (17.5, 4), (17.5, 13.5), (2.5, 13.5), close=True)
    pen.path((10, 13.5), (10, 17))
    pen.path((7, 17), (13, 17))


@_register("projects")
def _(pen: _Pen) -> None:
    pen.path((2.5, 6), (8, 6), (9.5, 8), (17.5, 8), (17.5, 16), (2.5, 16), close=True)


@_register("settings")
def _(pen: _Pen) -> None:
    pen.circle(10, 10, 3.6, filled=False)
    for index in range(6):
        angle = index * math.pi / 3
        inner, outer = 6.2, 8.8
        pen.path((10 + inner * math.cos(angle), 10 + inner * math.sin(angle)),
                 (10 + outer * math.cos(angle), 10 + outer * math.sin(angle)))


@_register("bolt")
def _(pen: _Pen) -> None:
    pen.path((11.5, 2.5), (5.5, 11), (9.5, 11), (8.5, 17.5), (14.5, 9), (10.5, 9),
             (11.5, 2.5), close=True)


@_register("clock")
def _(pen: _Pen) -> None:
    pen.circle(10, 10, 7.4, filled=False)
    pen.path((10, 5.4), (10, 10.4), (13.6, 12.2))


@_register("database")
def _(pen: _Pen) -> None:
    # Cilindro de três andares: duas elipses fechadas e as laterais. É a forma do objeto, não
    # um retângulo com linhas dentro.
    for y in (5.4, 10, 14.6):
        pen.arc((10, y), (7.5, 2.7), 180, 180)
        pen.arc((10, y), (7.5, 2.7), 0, 180)
    pen.path((2.5, 5.4), (2.5, 14.6))
    pen.path((17.5, 5.4), (17.5, 14.6))


@_register("plus")
def _(pen: _Pen) -> None:
    pen.path((10, 4), (10, 16))
    pen.path((4, 10), (16, 10))


@_register("send")
def _(pen: _Pen) -> None:
    pen.path((4, 10), (16, 10))
    pen.path((10.5, 4.5), (16, 10), (10.5, 15.5))


@_register("stop")
def _(pen: _Pen) -> None:
    pen.rect(5.5, 5.5, 14.5, 14.5)


@_register("mic")
def _(pen: _Pen) -> None:
    pen.arc((10, 8), (3.6, 5), 0, 180)
    pen.path((10, 13), (10, 16))
    pen.path((6.5, 16), (13.5, 16))


@_register("clip")
def _(pen: _Pen) -> None:
    pen.path((13.5, 5.5), (6.5, 12.5), (6.5, 15.5), (15.5, 6.5), (15.5, 3.5), (8.5, 10.5))


@_register("chevron")
def _(pen: _Pen) -> None:
    pen.path((7.5, 4.5), (13, 10), (7.5, 15.5))


@_register("chevron-down")
def _(pen: _Pen) -> None:
    pen.path((4.5, 7.5), (10, 13), (15.5, 7.5))


@_register("info")
def _(pen: _Pen) -> None:
    pen.circle(10, 10, 7.8, filled=False)
    pen.path((10, 9), (10, 14.6))
    pen.circle(10, 6.2, 1.1)


@_register("user")
def _(pen: _Pen) -> None:
    pen.circle(10, 7.4, 3.4)
    pen.arc((10, 18.5), (6.5, 6.5), 0, 180)


@_register("check")
def _(pen: _Pen) -> None:
    pen.path((4.5, 10.5), (8.5, 14.5), (15.5, 5.5))


@_register("refresh")
def _(pen: _Pen) -> None:
    pen.arc((10, 10), (6.6, 6.6), 40, 265)
    pen.path((15.6, 4.2), (16.4, 8.2), (12.4, 7.4))


@_register("close")
def _(pen: _Pen) -> None:
    pen.path((5, 5), (15, 15))
    pen.path((15, 5), (5, 15))


@_register("minimize")
def _(pen: _Pen) -> None:
    pen.path((4.5, 14), (15.5, 14))


@_register("maximize")
def _(pen: _Pen) -> None:
    pen.rect(5, 5, 15, 15, filled=False)


def draw(canvas: tk.Canvas, name: str, cx: float, cy: float, size: int, color: str,
         *, tags: str | None = None) -> str:
    """Desenha o glifo `name` centrado em (cx, cy). Retorna a tag para apagar depois."""
    try:
        glyph = _GLYPHS[name]
    except KeyError:
        raise KeyError(f"glifo desconhecido: {name} (existentes: {sorted(_GLYPHS)})") from None
    tag = tags or f"glyph:{name}"
    glyph(_Pen(canvas, cx, cy, size, color, tag))  # type: ignore[operator]
    return tag


def erase(canvas: tk.Canvas, tag: str) -> None:
    canvas.delete(tag)


def glyph_names() -> list[str]:
    return sorted(_GLYPHS)


_TRAILS = ((-6.5, -7.0, 5.5, 5.0, 1.55), (-1.5, -8.5, 8.0, 1.0, 2.3),
           (3.5, -6.0, 9.5, -0.5, 1.35))


def meteor_mark(canvas: tk.Canvas, cx: float, cy: float, size: int, *, head: str,
                tail: str, tags: str = "brand") -> None:
    """Marca da Lunmi: três rastros diagonais com cabeça, simplificados para 16–32 px.

    Geometria — linha e círculo — não ilustração. Sem brilho nem halo: o rastro sai do próprio
    ponto e o fundo escuro faz o resto.
    """
    scale = size / 20.0
    for x0, y0, x1, y1, head_radius in _TRAILS:
        canvas.create_line(cx + x0 * scale, cy + y0 * scale, cx + x1 * scale, cy + y1 * scale,
                           fill=tail, width=max(1.0, 1.5 * scale), capstyle="round", tags=tags)
        radius = head_radius * scale
        canvas.create_oval(cx + x1 * scale - radius, cy + y1 * scale - radius,
                           cx + x1 * scale + radius, cy + y1 * scale + radius,
                           fill=head, outline=head, width=0, tags=tags)


def starfield(canvas: tk.Canvas, width: int, height: int, *, color: str, seed: int = 7,
              count: int = 26, tags: str = "stars") -> None:
    """Pontos esparsos e fixos. A spec pede 'raríssimos'; 26 numa janela 1280×800 é raro.

    Semente fixa de propósito: aleatório por sessão faria a janela trocar de céu entre aberturas e
    tornaria qualquer captura não reproduzível.
    """
    rng = random.Random(seed)
    canvas.delete(tags)
    for _ in range(count):
        x = rng.uniform(4, max(5.0, width - 4))
        y = rng.uniform(4, max(5.0, height - 4))
        radius = rng.choice((0.6, 0.9, 1.3))
        canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color,
                           outline=color, width=0, tags=tags)


def meteor_streak(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, *,
                  tail: str, head: str, tags: str = "streak") -> None:
    """Um rastro longo de canto: o detalhe cósmico da referência, numa espessura só."""
    canvas.create_line(x0, y0, x1, y1, fill=tail, width=2, capstyle="round", tags=tags)
    canvas.create_oval(x1 - 2.2, y1 - 2.2, x1 + 2.2, y1 + 2.2, fill=head, outline=head,
                       width=0, tags=tags)
