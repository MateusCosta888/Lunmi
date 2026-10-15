"""Compositor multilinha da Lunmi, atendendo ainda pela assinatura do `Entry`.

Doze chamadores fora desta entrega fazem `input.get()`, `input.insert(0, ...)` e
`input.delete(0, "end")`. São três chamadas de `Entry` num widget que agora é `Text` — o campo
precisa de linhas. O adaptador abaixo converte o índice e nada mais; reescrever doze arquivos
para casar com a nova assinatura seria trocar doze lugares por três linhas.

Por que o índice muda: `Entry` numera caracteres a partir de 0; `Text` não tem "0", o início é
"1.0". Um `insert(0, ...)` repassado cru levanta TclError na primeira linha de qualquer smoke.

Rascunho durante execução: o campo continua editável (spec §7.2 — "preservar rascunho"). O que
bloqueia é o despacho, e ele já está guardado em `ChatWindow.submit()`, que retorna quando há
tarefa em voo. Deixar o campo legível enquanto ele é usável é o que a tela tem de dizer.
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from app.ui import theme

MIN_LINES = 1
MAX_LINES = 6
# 12 caracteres pedidos, não os 80 padrão do `Text`: com `fill="x"` a largura real é alocada pelo
# gerenciador, e o pedido só alimenta a conta de quanto a doca exige. Medido com o padrão: o
# compositor pedia 741 px, a doca 912, e a janela inteira era declarada cortada em 1280 px.
MIN_CHARS = 12
PLACEHOLDER = "Digite sua mensagem para a Lunmi..."
SHIFT_MASK = 0x0001


def entry_anchor(index: object) -> str:
    """Índice de `Entry` (offset na primeira linha) convertido para índice de `Text`.

    Índices que já são de `Text` — `"end"`, `"insert"`, `"1.4"` — passam intactos: o adaptador
    não adivinha, ele traduz só o que um `Entry` saberia dizer.
    """
    if isinstance(index, int):
        return f"1.{index}"
    text = str(index)
    return "1.0" if text == "0" else text


class Composer(tk.Text):
    """Campo de rascunho que cresce até `max_lines` e depois rola por dentro."""

    def __init__(self, parent: tk.Misc, *, on_submit: Callable[[], object],
                 max_lines: int = MAX_LINES, name: str = "input") -> None:
        super().__init__(parent, name=name, wrap="word", width=MIN_CHARS, height=MIN_LINES,
                         relief="flat", borderwidth=0, padx=theme.SPACE_4, pady=theme.SPACE_3,
                         font=theme.F_BODY, fg=theme.TEXT_PRIMARY, bg=theme.BG_INPUT,
                         insertbackground=theme.ACCENT, highlightthickness=0, undo=False,
                         cursor="xterm", selectbackground=theme.ACCENT,
                         selectforeground=theme.ON_ACCENT, takefocus=True)
        self._on_submit = on_submit
        self._max_lines = max_lines
        self._fit_job: str | None = None
        self._in_modified = False
        # O placeholder é um rótulo por cima, não texto no buffer: `get()` jamais devolve a dica,
        # então um envio com o campo vazio continua vazio em vez de mandar "Digite sua mensagem".
        self._hint = tk.Label(self, text=PLACEHOLDER, bg=theme.BG_INPUT, fg=theme.TEXT_MUTED,
                              font=theme.F_BODY, takefocus=False, pady=0)
        self._hint.bind("<Button-1>", lambda _e: self.focus_set())
        self._show_hint()
        self.bind("<Return>", self._enter)
        self.bind("<KP_Enter>", self._enter)
        # `Tab` na classe do `Text` insere "\t" e fica com o foco; `Shift-Tab` devolve "break" e
        # não faz nada. Num campo de duas linhas isso prende a navegação de teclado: os dois
        # padrões viram próxima/anterior, e o caractere de tabulação continua alcançável por
        # Ctrl+V ou por quem o digitar explicitamente.
        self.bind("<Tab>", self._focus_next)
        self.bind("<Shift-Tab>", self._focus_previous)
        self.bind("<<Modified>>", self._on_modified)
        self.bind("<<Paste>>", lambda _e: self._schedule_fit())
        self.bind("<FocusIn>", lambda _e: self._paint_focus(True))
        self.bind("<FocusOut>", lambda _e: self._paint_focus(False))
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Configure>", lambda _e: self._schedule_fit())
        self._fit()

    # ------------------------------------------------------------- API do Entry --
    def get(self, *indices: str) -> str:  # type: ignore[override]
        # Assinatura de `Entry` (sem índice) é o contrato dos callers; com índices, o
        # comportamento cru do `Text` continua disponível.
        return tk.Text.get(self, *(indices or ("1.0", "end-1c")))

    def insert(self, index: object, chars: str,
               *args: str | list[str] | tuple[str, ...]) -> None:
        tk.Text.insert(self, entry_anchor(index), chars, *args)

    def delete(self, first: object, last: object = None) -> None:
        if last is None:
            tk.Text.delete(self, entry_anchor(first))
        else:
            tk.Text.delete(self, entry_anchor(first), entry_anchor(last))

    # ------------------------------------------------------------------- teclado --
    def _enter(self, event: tk.Event) -> str:
        if int(event.state) & SHIFT_MASK:
            tk.Text.insert(self, "insert", "\n")
        else:
            self._on_submit()
        # "break" antes da binding de classe do `Text`, que inseriria a linha por conta própria no
        # Enter simples — sem isto, cada envio também deixaria um parágrafo vazio no rascunho.
        return "break"

    def _travel(self, command: str) -> None:
        """Travessia de foco pelo mecanismo do Tk.

        `focus_next()`/`focus_prev()` não existem no Tkinter desta máquina (medido: AttributeError
        dentro da binding, que engolia o `break` e deixava a tabulação entrar). Quem sabe a ordem é
        o próprio Tk: `tk_focusNext` devolve o caminho do próximo widget focável, ou vazio.
        """
        try:
            nxt = self.tk.call(command, str(self))
        except tk.TclError:
            return
        if nxt:
            # `focus -force <caminho>` como comando global: chamado como método do widget, o
            # Tkinter resolve o caminho para o objeto e reclama que `focus` não é subcomando dele
            # (medido num `ttk.Button`: "must be configure, cget, invoke, instate, state…").
            self.tk.call("focus", "-force", nxt)

    def _focus_next(self, event: tk.Event) -> str:
        self._travel("tk_focusNext")
        return "break"

    def _focus_previous(self, event: tk.Event) -> str:
        self._travel("tk_focusPrev")
        return "break"

    def _on_modified(self, *_: object) -> None:
        """Todo conteúdo que entra — tecla, colagem, `insert(0, …)` de um teste — acerta a altura.
        Limpar a flag para receber o próximo evento; a própria limpeza mexe na flag, daí o guarda:
        sem ele os dois eventos se chamam um ao outro.
        """
        if self._in_modified:
            return
        self._in_modified = True
        try:
            self.edit_modified(False)
        finally:
            self._in_modified = False
        self._fit()

    def _wheel(self, event: tk.Event) -> str:
        top, bottom = self.yview()
        if bottom < 1.0 or top > 0.0:
            self.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def destroy(self) -> None:
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
            self._fit_job = None
        self._hint.destroy()   # neto do `Text`: destruído o pai, o filho não fica órfão.
        super().destroy()

    # --------------------------------------------------------------------- altura --
    def _schedule_fit(self) -> None:
        if self._fit_job is None:
            self._fit_job = self.after_idle(self._fit)

    def _fit(self, *_: object) -> None:
        """Linhas *exibidas*, não digitadas: uma frase longa quebra e o campo acompanha."""
        self._fit_job = None
        try:
            if self.winfo_width() <= 1:
                return
            counted = self.count("1.0", "end", "update", "displaylines")
        except tk.TclError:
            return      # janela fechada no meio de um `after_idle` é caminho normal
        lines = counted if isinstance(counted, int) else (counted[0] if counted else 0)
        wanted = max(MIN_LINES, min(max(1, lines), self._max_lines))
        if int(self.cget("height")) != wanted:
            self.configure(height=wanted)
        self._paint_focus(bool(self.focus_get() is self))

    # ------------------------------------------------------------------- estado --
    def hint_visible(self) -> bool:
        """Se a dica está na tela. Leitura pública: a medição não abre o privado do widget."""
        return bool(self._hint.winfo_ismapped())

    def set_outline(self, color: str, border: int = 1) -> None:
        """Delegado à superfície da caixa: quem desenha a borda é o `theme.Surface` pai.

        O campo não tem moldura própria — a moldura é o raio desenhado em volta dele —, então o
        foco sobe até a superfície que o hospeda. Não achou nenhuma: não pinta nada.
        """
        node: tk.Misc | None = self.master
        while isinstance(node, tk.BaseWidget):
            if isinstance(node, theme.Surface):
                node.set_outline(color, border)
                return
            node = node.master

    def _show_hint(self) -> None:
        """Origem (0,0), não (padx, pady): filho de um `Text` é posicionado relativo à **área de
        texto**. Medido: `place(16, 12)` caía em (32, 24) — a dica uma linha abaixo do cursor.
        """
        self._hint.place(x=0, y=0)

    def _paint_focus(self, focused: bool, *_: object) -> None:
        """Foco visível sem borda neon: 2 px da cor de destaque, e a dica só existe no vazio.

        A dica é um rótulo opaco por cima da primeira linha. Escondê-la só na entrada de foco
        deixava o texto digitado coberto — medido na captura de 700x560 com rascunho de três
        linhas: a primeira linha não aparecia. Quem tem de sumir é a dica quando há texto.
        """
        if self.get() or focused:
            self._hint.place_forget()
        else:
            self._show_hint()
        self.set_outline(theme.ACCENT if focused else theme.BORDER_SUBTLE,
                         2 if focused else 1)
