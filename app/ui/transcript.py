"""Transcurso da Lunmi: uma bolha arredondada por mensagem.

Por que não um `Text` só para o histórico inteiro, que foi a primeira forma: medido em seis
variantes (`docs/design/probes/tint_probe.py`), o `background` de uma tag do Tk 8.6 preenche a
linha exibida de ponta a ponta, e nenhuma combinação de `lmargin`/`rmargin`, de ordem de tags ou de
newline faz a tinta seguir a coluna. A faixa era larga, o texto não. Aqui a caixa **é** o widget,
então a área pintada não pode mais divergir da coluna.

Por que a bolha é arredondada assim: o `Text` não tem raio, e um `Frame` com um `Text` dentro por
cima de um fundo não arredonda nada. Então o corpo é o próprio `Text`, e ele é o contêiner da
`theme.Surface` que desenha o raio — o que exige `inner_factory`, porque o pai do widget tem de ser
o Canvas da superfície e o Canvas só existe depois de `super().__init__`.

O recuo de `BUBBLE_INSET` existe por geometria, não por gosto: com raio 12, um canto do Text a 3 px
ficaria fora do círculo (distância 12,73 > 12) e apareceria como um quadrado claro no ombro da
bolha; a 5 px, some dentro (9,90 < 12).

Leitura pelo mecanismo nativo: cada corpo fica `state="disabled"` depois de preenchido. Medido
nesta plataforma (`docs/design/probes/card_edit_probe.py`): desabilitado ainda seleciona
arrastando, ainda atende Ctrl+C, e Ctrl+D/K/O/W não alteram nada.
"""
from __future__ import annotations

import re
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import cast

from app.ui import theme

BUBBLE_RADIUS = 16
BUBBLE_INSET = 7
BUBBLE_PAD_X = 12
BUBBLE_PAD_Y = 10
BLOCK_MAX = 560
EDGE = 10

# O veredito vem antes da frase e colorido: quem varre o histórico procura o estado do turno, não
# lê 400 caracteres para descobrir se deu certo. A palavra continua escrita — a cor acompanha.
_VERDICT = re.compile(r"^(SUCCESS|NOT_VERIFIED|FAILED|CANCELLED):")
_VERDICT_TAGS = {"SUCCESS": "verdict_success", "NOT_VERIFIED": "verdict_warning",
                 "FAILED": "verdict_danger", "CANCELLED": "verdict_neutral"}
_VERDICT_COLORS = {"verdict_success": theme.SUCCESS, "verdict_warning": theme.WARNING,
                   "verdict_danger": theme.ERROR, "verdict_neutral": theme.TEXT_MUTED}
# O mesmo tom vale para a palavra e para a borda da bolha de resultado. Cor e palavra juntas:
# estado só por cor não lê para quem não distingue, e palavra só não é varredura rápida.
VERDICT_TONES = {"SUCCESS": theme.SUCCESS, "NOT_VERIFIED": theme.WARNING,
                 "FAILED": theme.ERROR, "CANCELLED": theme.TEXT_MUTED}


def is_measurement(line: str) -> bool:
    """Duas ou mais vírgulas-tipografia " · " no mesmo traço é medição, não prosa.

    A linha de tempos sai do bloco colorido e vai para `metric`: é evidência, e evidência se lê em
    coluna alinhada, não embrulhada na caixa da fala.
    """
    return line.count(" · ") >= 2


class Transcript(tk.Frame):
    """Histórico rolável com uma bolha por mensagem, usuário à direita e assistente à esquerda."""

    CARD_LIMIT = 200          # bolhas vivas
    LINE_LIMIT = 2000         # linhas lógicas vivas, o mesmo orçamento da versão anterior

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, bg=theme.BG_CHAT, name="transcript")
        self.canvas = tk.Canvas(self, bg=theme.BG_CHAT, highlightthickness=0, bd=0,
                                takefocus=False)
        # `ttk`, não `tk`: a barra nativa do Windows desenha o trilho em cinza claro e ignora `bg`
        #/`troughcolor` — na captura de 13/10 ela era o único objeto claro da tela escura. O tema
        # aplicado em `theme.apply` é quem veste a barra.
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        def scrolled(first: float, last: float) -> None:
            bar.set(first, last)
            self._visible_bodies()
        self.canvas.configure(yscrollcommand=scrolled)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.cards: list[theme.Surface] = []
        self.bodies: list[tk.Text] = []
        self._kinds: list[str] = []
        self._line_count = 0
        self._items: list[int] = []
        self._positions: list[int] = []
        self._reflow_job: str | None = None
        self._body_font = tkfont.Font(root=self, font=theme.F_BODY)
        self._char = self._body_font.measure("0") or 8
        self._natural_widths: dict[tk.Text, int] = {}
        for widget in (self, self.canvas):
            widget.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    # ------------------------------------------------------------------ escrita --
    def append(self, speaker: str, text: str) -> None:
        kind = {"Você": "user", "Agente": "agent"}.get(speaker, "system")
        body, card = self._make_card(kind, self._block_chars())
        body.insert("end", f"{speaker}  {time.strftime('%H:%M')}\n", "who")
        for line in text.split("\n"):
            # `index("end")` aponta uma linha depois de onde `insert("end", …)` cai; o único
            # anchor que coincide com o texto que entra é "end-1c".
            start = body.index("end-1c")
            body.insert("end", f"{line}\n", "metric" if is_measurement(line) else ())
            verdict = _VERDICT.match(line)
            if verdict:
                body.tag_add(_VERDICT_TAGS[verdict.group(1)], start,
                             f"{start}+{len(verdict.group(1))}c")
        body.configure(state="disabled")
        self._commit(kind, body, card)

    def result(self, verdict: str, headline: str, measures: list[str]) -> None:
        """Cartão de turno: veredito, frase e as medições reais do `TaskResult`.

        Quem escreveu o texto é o `ChatWindow`, que tem o `TaskResult` na mão; aqui só existe a
        apresentação do que ele tem. Nada de percentual em voo, nada de streaming simulado — as
        linhas de medição são as que o controller mediu, na ordem em que ele as mediu.
        """
        body, card = self._make_card("result", self._block_chars())
        tone = VERDICT_TONES.get(verdict, theme.TEXT_MUTED)
        body.tag_configure("verdict", font=theme.F_BODY_MEDIUM, foreground=tone, spacing3=8)
        body.insert("end", f"{verdict}  {time.strftime('%H:%M')}\n", "verdict")
        body.insert("end", f"{headline}\n", ())
        for line in measures:
            visible = line.startswith(("falha:", "memória:")) or "sem evidência" in line
            body.insert("end", f"{line}\n", () if visible else "metric")
        body.tag_configure("metric", elide=True)
        expanded = False

        def toggle() -> None:
            nonlocal expanded
            expanded = not expanded
            body.tag_configure("metric", elide=not expanded)
            details.configure(text="Ocultar métricas" if expanded else "Ver métricas")
            self._size_body(body)
            card.relayout()
            self._sync_scroll()

        details = ttk.Button(body, text="Ver métricas", command=toggle,
                             style="Lunmi.Action.TButton")
        body.window_create("end", window=details)
        body.insert("end", "\n")
        body.configure(state="disabled")
        card.set_outline(tone, 1)
        self._commit("result", body, card)

    def _commit(self, kind: str, body: tk.Text, card: theme.Surface) -> None:
        if kind in {"user", "agent"}:
            body.configure(width=self._message_chars(body, self._block_chars()))
        item = self.canvas.create_window(0, 0, anchor="nw", window=card)
        self._items.append(item)
        # Altura em linhas *exibidas* só existe depois do layout na largura final. Sem este
        # update_idletasks, uma bolha criada antes do primeiro <Configure> media a quebra de uma
        # coluna de 20 caracteres e pedia 5.011 px de altura — medido, não previsto.
        self.canvas.update_idletasks()
        self._size_body(body)
        self.canvas.update_idletasks()
        card.relayout()
        self.cards.append(card)
        self.bodies.append(body)
        self._kinds.append(kind)
        self._line_count += self._card_lines(body)
        self._trim()
        self._sync_scroll()
        self.canvas.yview_moveto(1.0)

    def _make_card(self, kind: str, chars: int) -> tuple[tk.Text, theme.Surface]:
        if kind == "system":
            # Narração não é fala de ninguém: sem tinta nem borda, um tom abaixo, na coluna de leitura.
            fill, outline, border = theme.BG_CHAT, "", 0
            font, foreground = theme.F_SMALL, theme.TEXT_MUTED
        elif kind == "user":
            fill, outline, border = theme.BG_USER, theme.BORDER_ON_USER, 1
            font, foreground = theme.F_BODY, theme.TEXT_PRIMARY
        else:
            fill, outline, border = theme.BG_CARD, theme.BORDER_SUBTLE, 1
            font, foreground = theme.F_BODY, theme.TEXT_PRIMARY
        muted = theme.TEXT_MUTED_ON_USER if kind == "user" else theme.TEXT_MUTED

        def build(parent: tk.Widget) -> tk.Text:
            body = tk.Text(parent, wrap="word", height=1, width=chars, relief="flat",
                           borderwidth=0, padx=BUBBLE_PAD_X, pady=BUBBLE_PAD_Y, font=font,
                           fg=foreground, bg=fill, insertbackground=theme.ACCENT,
                           highlightthickness=0, takefocus=False, undo=False, cursor="xterm",
                           selectbackground=theme.ACCENT, selectforeground=theme.ON_ACCENT)
            body.tag_configure("who", font=theme.F_SMALL, foreground=muted, spacing3=8,
                               justify="right" if kind == "user" else "left")
            body.tag_configure("metric", font=theme.F_DATA, foreground=muted, spacing1=4)
            for name, color in _VERDICT_COLORS.items():
                body.tag_configure(name, font=theme.F_BODY_MEDIUM, foreground=color)
            body.bind("<MouseWheel>", self._on_wheel)
            return body

        card = theme.Surface(self.canvas, fill=fill, outline=outline, border=border,
                             radius=BUBBLE_RADIUS, background=theme.BG_CHAT, fit=True,
                             padding=(BUBBLE_INSET, BUBBLE_INSET, BUBBLE_INSET, BUBBLE_INSET),
                             inner_factory=build)
        return cast(tk.Text, card.inner), card

    @staticmethod
    def _card_lines(body: tk.Text) -> int:
        """Definição única de "linha": linhas com conteúdo no corpo. append e trim somam/subtraem
        exatamente isto — contar uma coisa e devolver outra fez o orçamento derivar 1 por descarte.
        """
        return max(0, int(body.index("end-1c").split(".")[0]) - 1)

    def _trim(self) -> None:
        """Descarta bolhas antigas completas. Nunca corta o meio de uma mensagem.

        Se sobrar uma única bolha acima do orçamento, ela fica inteira: truncar calado conteúdo do
        usuário seria pior que o orçamento estourado, e o limite é nosso.
        """
        while (len(self.cards) > self.CARD_LIMIT or self._line_count > self.LINE_LIMIT) \
                and len(self.cards) > 1:
            card, body = self.cards.pop(0), self.bodies.pop(0)
            self._kinds.pop(0)
            self._natural_widths.pop(body, None)
            self.canvas.delete(self._items.pop(0))
            self._line_count -= self._card_lines(body)
            card.destroy()

    def _size_body(self, body: tk.Text) -> None:
        """Altura = linhas *exibidas*: texto longo quebra dentro da bolha."""
        counted = body.count("1.0", "end", "update", "displaylines")
        lines = counted if isinstance(counted, int) else (counted[0] if counted else 0)
        height = max(1, lines)
        if int(body.cget("height")) != height:
            body.configure(height=height)

    # ------------------------------------------------------------------- layout --
    def _block_chars(self) -> int:
        """Largura da coluna de leitura em caracteres médios, descontado o que a bolha consome.

        O recuo da superfície e o padding do texto saem da conta: a tinta segue a coluna, e a
        coluna é o que resta depois da moldura desenhada.
        """
        width = self.canvas.winfo_width() or self.winfo_width()
        if width <= 1:
            width = BLOCK_MAX + 2 * EDGE
        usable = max(160, width - 2 * EDGE)
        taken = 2 * (BUBBLE_PAD_X + BUBBLE_INSET + 1)
        return max(12, (min(usable, BLOCK_MAX) - taken) // self._char)

    def _message_chars(self, body: tk.Text, maximum: int) -> int:
        natural = self._natural_widths.get(body)
        if natural is None:
            longest = max((self._body_font.measure(line)
                           for line in body.get("1.0", "end-1c").splitlines()), default=0)
            natural = max(18, (longest + self._char - 1) // self._char + 2)
            self._natural_widths[body] = natural
        return min(maximum, natural)

    def _on_canvas_configure(self, event: tk.Event) -> None:
        if int(event.width) <= 1:
            return
        self._schedule_reflow()

    def _schedule_reflow(self) -> None:
        """Redimensionar empilha configure atrás de configure; um passe por quadro basta."""
        if self._reflow_job is not None:
            return
        self._reflow_job = self.after(30, self._reflow)

    def _reflow(self) -> None:
        self._reflow_job = None
        width = self.canvas.winfo_width()
        if width <= 1:
            return
        chars = self._block_chars()
        changed: list[tuple[tk.Text, theme.Surface]] = []
        for kind, body, card in zip(self._kinds, self.bodies, self.cards):
            wanted = self._message_chars(body, chars) if kind in {"user", "agent"} else chars
            if int(body.cget("width")) != wanted:
                body.configure(width=wanted)
                changed.append((body, card))
        # Let Tk settle all widths once; flushing each Text recursively stalls live resize.
        if not changed:
            self._sync_scroll()
        if changed:
            self._reflow_job = self.after_idle(lambda: self._finish_reflow(changed))

    def _finish_reflow(self, changed: list[tuple[tk.Text, theme.Surface]]) -> None:
        self._reflow_job = None
        for body, card in changed:
            if body.winfo_exists():
                self._size_body(body)
                card.relayout()
        self._sync_scroll()

    def _visible_bodies(self) -> None:
        if not hasattr(self, "cards"):
            return
        top = self.canvas.canvasy(0) - 100
        bottom = top + self.canvas.winfo_height() + 200
        for card, item, y in zip(self.cards, self._items, self._positions):
            visible = y < bottom and y + card.winfo_reqheight() > top
            wanted = "normal" if visible else "hidden"
            if self.canvas.itemcget(item, "state") != wanted:
                self.canvas.itemconfigure(item, state=wanted)

    def _sync_scroll(self) -> None:
        width = self.canvas.winfo_width()
        y = 0
        self._positions = []
        for kind, card, item in zip(self._kinds, self.cards, self._items):
            w, h = card.winfo_reqwidth(), card.winfo_reqheight()
            x = max(0, width - w) if kind == "user" else 0
            self._positions.append(y)
            self.canvas.coords(item, x, y)
            self.canvas.itemconfigure(item, width=w, height=h)
            y += h + theme.SPACE_3
        self.canvas.configure(scrollregion=(0, 0, width, y))
        self._visible_bodies()

    def card_y(self, index: int) -> int:
        return self._positions[index]

    def _on_wheel(self, event: tk.Event) -> str:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    # ------------------------------------------------------------------ leitura --
    def kinds(self) -> list[str]:
        """Quem é cada bolha viva, na ordem visual — para a medição não abrir o privado."""
        return list(self._kinds)

    def line_count(self) -> int:
        """Linhas vivas contabilizadas pelo orçamento."""
        return self._line_count

    def text(self) -> str:
        """O que está na tela, na ordem visual. Não é cache nem modelo paralelo."""
        return "\n".join(body.get("1.0", "end-1c") for body in self.bodies)

    def destroy(self) -> None:
        if self._reflow_job is not None:
            try:
                self.after_cancel(self._reflow_job)
            except tk.TclError:
                pass
            self._reflow_job = None
        super().destroy()
