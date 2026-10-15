"""Painel direito da Lunmi: ações rápidas, tarefas recentes e resumo de memória.

A regra da etapa é não inventar capacidade nenhuma. Ação rápida **preenche o compositor e foca** —
não envia sozinha, porque enviar é decisão do usuário. "Recentes" só mostra o que rodou nesta
sessão, com o veredito real; quando não rodou nada, diz isso. O resumo de memória faz uma leitura
fora da thread Tk e obedece a flag `enabled` **antes** de listar (B3 — `MemoryManager.list` não
verifica a flag), e descarta resposta atrasada por geração.

Destino assíncrono próprio (B2): o painel é o `sink` das suas chamadas, com `closing` e
`winfo_exists()` como a janela de memória já tem, então uma resposta que chega depois de o painel
ter sido destruído não toca Tcl morto nem reescreve tela.
"""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from app.core.controller import TaskResult
from app.ui import shell, theme, ui_preferences
from app.ui.transcript import VERDICT_TONES

RECENT_LIMIT = 6
MEMORY_LIMIT = 8
# A barra de rolagem do `clam` mede 17-18 px; arredondado a 20 porque o que a
# varredura de corte compara é o **pedido** do contêiner, e pedido maior que a
# coluna é corte declarado.
SCROLLBAR_GUTTER = 20

# Os atalhos padrão moram em `ui_preferences`, porque são o que o editor mostra quando o
# arquivo de preferências não existe ou está corrompido — uma lista aqui seria uma segunda fonte
# de verdade.
QUICK_ACTIONS = tuple((action.label, action.draft) for action in ui_preferences.DEFAULT_ACTIONS)


class RightPanel(tk.Frame):
    """As três seções da coluna direita, vivas dentro da janela principal."""

    def __init__(self, parent: tk.Misc, *, on_quick_action: Callable[[str], object],
                 on_open_memory: Callable[[], object], memory_call: Callable[..., object],
                 snapshot: Callable[[], Any],
                 on_details: Callable[[TaskResult], object] | None = None,
                 actions: tuple[ui_preferences.QuickAction, ...] | None = None,
                 on_actions: Callable[[tuple[ui_preferences.QuickAction, ...]], object]
                 | None = None, on_edit: Callable[[], object] | None = None) -> None:
        super().__init__(parent, background=theme.BG_WINDOW, name="panels")
        self.closing = False
        self._on_quick_action = on_quick_action
        self._on_open_memory = on_open_memory
        self._memory_call = memory_call
        self._snapshot = snapshot
        self._on_details = on_details
        self._on_actions = on_actions
        self._on_edit = on_edit
        self._actions = tuple(actions if actions is not None
                              else ui_preferences.DEFAULT_ACTIONS)
        self._generation = 0
        self._recent: list[tuple[str, TaskResult]] = []
        self.pack(fill="both", expand=True, padx=theme.SPACE_2, pady=theme.SPACE_2)
        # O conteúdo do painel é maior do que qualquer coluna de 292 px que a janela possa dar:
        # três cartões, quatro botões e seis linhas. Sem rolagem própria, o `place` da casca
        # simplesmente cortava o último cartão — medido em 1280x800, `right_panel` pedia 394x835
        # e recebia 292x768. Então o painel é uma superfície rolável, como o transcurso.
        self.canvas = tk.Canvas(self, background=theme.BG_WINDOW, highlightthickness=0, bd=0,
                                width=theme.PANEL_WIDTH - 2 * theme.SPACE_2 - SCROLLBAR_GUTTER,
                                takefocus=False)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self._slot = tk.Frame(self.canvas, background=theme.BG_WINDOW)
        self._viewport = self.canvas.create_window(0, 0, anchor="nw", window=self._slot)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_configure)
        self._slot.bind("<Configure>", lambda _e: self._sync())
        for widget in (self, self.canvas, self._slot):
            widget.bind("<MouseWheel>", self._on_wheel)

        self._cards: list[shell.PanelCard] = []
        self._actions_card = shell.PanelCard(self._slot, title="Ações rápidas", icon="bolt",
                                             name="panel_actions",
                                             link_text="editar" if on_actions else "",
                                             on_link=self.open_editor if on_actions else None)
        card = self._actions_card
        card.pack(fill="x", pady=(0, theme.SPACE_3))
        note = tk.Label(card.body, text="Preenchem o campo abaixo; nada é enviado sozinho.",
                        font=theme.F_SMALL, background=theme.BG_CARD_SUBTLE,
                        foreground=theme.TEXT_MUTED, wraplength=theme.PANEL_WIDTH - 70,
                        justify="left")
        note.pack(anchor="w", pady=(0, theme.SPACE_2))
        self._actions_body = card.body
        self._build_action_buttons()

        recent = shell.PanelCard(self._slot, title="Tarefas recentes", icon="clock",
                                 name="panel_recent")
        recent.pack(fill="x", pady=(0, theme.SPACE_3))
        self._recent_body = recent.body
        self._show_recent_placeholder()

        memory = shell.PanelCard(self._slot, title="Memória", icon="database",
                                 name="panel_memory", link_text="Gerenciar",
                                 on_link=self.open_memory)
        memory.pack(fill="x")
        self._cards = [card, recent, memory]
        self._memory_status = tk.Label(memory.body, text="Carregando…", font=theme.F_SMALL,
                                       background=theme.BG_CARD_SUBTLE,
                                       foreground=theme.TEXT_MUTED, wraplength=theme.PANEL_WIDTH - 70,
                                       justify="left")
        self._memory_status.pack(anchor="w")
        self._memory_body = memory.body
        self.refresh_memory()

    # ------------------------------------------------------------------- layout --
    def _relayout_cards(self) -> None:
        """Depois de mudar conteúdo, a superfície do cartão tem de ser recontada.

        `fit="height"` resolve o cartão no primeiro layout; nada avisa a `Surface` que o corpo
        cresceu depois. Sem esta chamada, o item de janela do cartão fica com a altura antiga e o
        Tk **desmapeia** o filho que cai fora dela — foi o que deixou o link "detalhes" existindo
        e invisível, e é o mesmo mecanismo que cortava a quarta ação rápida na captura.
        """
        for card in self._cards:
            card.relayout()
        self._sync()

    # ------------------------------------------------------------------- ações --
    def _build_action_buttons(self) -> None:
        """Os botões são o estado; reconstruir é mais barato do que sincronizar duas listas."""
        for child in self._actions_body.winfo_children():
            if isinstance(child, ttk.Button):
                child.destroy()

        def queue(text: str) -> Callable[[], None]:
            # Fábrica em vez de `lambda text=draft: …` no laço: a captura tem de ser do valor
            # daquela linha, e o tipo explícito é o que o mypy consegue inferir aqui.
            def run() -> None:
                self._on_quick_action(text)
            return run

        for action in self._actions:
            button = ttk.Button(self._actions_body, text=action.label, command=queue(action.draft),
                                style="Lunmi.Action.TButton", cursor="hand2")
            button.pack(fill="x", pady=(0, theme.SPACE_2))
        self._relayout_cards()

    def set_actions(self, actions: tuple[ui_preferences.QuickAction, ...]) -> None:
        self._actions = tuple(actions)[:ui_preferences.MAX_ACTIONS]
        self._build_action_buttons()

    def open_editor(self) -> None:
        if self._on_actions is None:
            return
        if self._on_edit is not None:
            self._on_edit()

    def _editor_saved(self, actions: tuple[ui_preferences.QuickAction, ...]) -> None:
        self._actions = actions
        self._build_action_buttons()
        if self._on_actions is not None:
            self._on_actions(actions)

    def open_memory(self) -> None:
        self._on_open_memory()

    # ---------------------------------------------------------------- recentes --
    def record(self, result: TaskResult) -> None:
        """Um turno real desta sessão entra na lista; nada é reconstruído de arquivo.

        A hora guardada é a de **chegada na tela**, não a do `TaskResult`: o contrato não traz
        timestamp, e inventar um seria a única data falsa de um cartão que existe para ser prova.
        """
        self._recent.insert(0, (time.strftime("%H:%M"), result))
        del self._recent[RECENT_LIMIT:]
        self._render_recent()

    def _show_recent_placeholder(self) -> None:
        tk.Label(self._recent_body,
                 text="Nenhuma tarefa nesta sessão. O histórico anterior não é recuperado aqui.",
                 font=theme.F_SMALL, background=theme.BG_CARD_SUBTLE,
                 foreground=theme.TEXT_MUTED, wraplength=theme.PANEL_WIDTH - 70,
                 justify="left").pack(anchor="w")
        self._relayout_cards()

    def _render_recent(self) -> None:
        for child in self._recent_body.winfo_children():
            child.destroy()
        if not self._recent:
            self._show_recent_placeholder()
            return
        for arrived_at, item in self._recent:
            row = tk.Frame(self._recent_body, background=theme.BG_CARD_SUBTLE)
            row.pack(fill="x", pady=(0, theme.SPACE_2))
            tone = VERDICT_TONES.get(item.status, theme.TEXT_MUTED)
            tk.Label(row, text=item.status, font=theme.F_BODY_MEDIUM,
                     background=theme.BG_CARD_SUBTLE, foreground=tone).pack(side="left")
            tk.Label(row, text=f"{item.duration_ms:.0f} ms · {arrived_at}", font=theme.F_SMALL,
                     background=theme.BG_CARD_SUBTLE,
                     foreground=theme.TEXT_MUTED).pack(side="right")
            detail = tk.Label(self._recent_body, text=clip(item.message), font=theme.F_SMALL,
                              background=theme.BG_CARD_SUBTLE, foreground=theme.TEXT_SECONDARY,
                              wraplength=theme.PANEL_WIDTH - 70, justify="left", anchor="w")
            detail.pack(fill="x")
            if self._on_details is not None:
                link = tk.Label(self._recent_body, text="detalhes", font=theme.F_SMALL,
                                background=theme.BG_CARD_SUBTLE, foreground=theme.ACCENT,
                                cursor="hand2")
                link.configure(takefocus=True)
                link.pack(anchor="w", pady=(4, theme.SPACE_2))
                target = item      # captura por linha: sem isto os quatro links abrem o último

                def show(_event: tk.Event, stored: TaskResult = target) -> None:
                    if self._on_details is not None:
                        self._on_details(stored)

                link.bind("<Button-1>", show)
                link.bind("<Return>", show)
                link.bind("<space>", show)
                link.bind("<FocusIn>", lambda event: event.widget.configure(
                    highlightthickness=1, highlightbackground=theme.ACCENT))
                link.bind("<FocusOut>", lambda event: event.widget.configure(highlightthickness=0))
        self._relayout_cards()

    # ----------------------------------------------------------------- memória --
    def refresh_memory(self) -> None:
        """Pede o estado atual. A flag `enabled` é lida antes de qualquer listagem (B3)."""
        generation = self._generation
        self._memory_call("panel_memory", self._snapshot, on_ok=lambda result: self._apply(
            result, generation), sink=self)

    def invalidate(self) -> None:
        """Wipe, desligar, apagar ou gravar: a leitura anterior deixa de valer na hora.

        A geração é o que mata a resposta atrasada: um `list()` que já estava no executor pode
        voltar depois do wipe, e sem isto ele reapresentaria o perfil que o usuário acabou de
        apagar.
        """
        # Limpar e pedir de novo, na mesma chamada. Deixar só "Atualizando…" era o painel preso:
        # nenhum outro caminho chama `refresh_memory`, então editar, apagar, desligar ou limpar a
        # memória não mostrava nunca o estado novo.
        self._generation += 1
        self._render_memory({"enabled": None, "items": []}, "Atualizando…")
        self.refresh_memory()

    def _apply(self, snapshot: Any, generation: int) -> None:
        if self.closing or generation != self._generation or not self.winfo_exists():
            return
        if not isinstance(snapshot, dict):
            self._render_memory({"enabled": None, "items": []},
                                "Memória indisponível nesta tarefa.")
            return
        self._render_memory(snapshot)

    def _render_memory(self, snapshot: dict[str, Any], force: str = "") -> None:
        for child in self._memory_body.winfo_children():
            if child is not self._memory_status:
                child.destroy()
        if force:
            self._memory_status.configure(text=force)
            return
        if not snapshot.get("available", True):
            self._memory_status.configure(text="Backend de memória não carregado ainda.")
            return
        if not snapshot.get("enabled"):
            self._memory_status.configure(text="Memória desligada nas preferências. "
                                               "Nada é lido nem lembrado.")
            return
        items = list(snapshot.get("items") or [])
        self._memory_status.configure(text=(f"{len(items)} lembranças ativas"
                                            if items else "Nada lembrado ainda."))
        for kind, text in items[:5]:
            tk.Label(self._memory_body, text=f"{kind} · {clip(text)}", font=theme.F_SMALL,
                     background=theme.BG_CARD_SUBTLE, foreground=theme.TEXT_SECONDARY,
                     wraplength=theme.PANEL_WIDTH - 70, justify="left", anchor="w").pack(fill="x")
        self._relayout_cards()

    # --------------------------------------------------------------------- vida --
    def _on_configure(self, event: tk.Event) -> None:
        if int(event.width) > 1:
            self.canvas.itemconfigure(self._viewport, width=int(event.width))
        self._sync()

    def _sync(self) -> None:
        region = self.canvas.bbox("all")
        if region:
            self.canvas.configure(scrollregion=region)

    def _on_wheel(self, event: tk.Event) -> str:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def set_status(self, text: str) -> None:
        """Destino das falas de memória (mesma porta da janela de memória, B2)."""
        self._memory_status.configure(text=text)

    def alive(self) -> bool:
        return not self.closing and bool(self.winfo_exists())

    def destroy(self) -> None:
        self.closing = True
        self._generation += 1
        super().destroy()


def clip(text: str, limit: int = 90) -> str:
    """Uma linha legível no painel estreito; o texto completo continua no transcurso."""
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"


class ActionEditor(ttk.Frame):
    """Editor dos atalhos: adicionar, renomear, reordenar e remover, com Salvar explícito.

    Trabalha numa lista local e só grava no "Salvar": fechar sem salvar não muda nada, que é o
    contrato de toda confirmação aqui. O texto de cada atalho é um rascunho para o compositor, não
    uma instrução completa — por isso o campo aceita terminar em espaço.
    """

    def __init__(self, parent: tk.Misc, actions: tuple[ui_preferences.QuickAction, ...],
                 on_save: Callable[[tuple[ui_preferences.QuickAction, ...]], object]) -> None:
        super().__init__(parent)
        self._items = list(actions)
        self._on_save = on_save
        frame = ttk.Frame(self, padding=theme.SPACE_5)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Estes botões escrevem no campo de mensagem. Nada é enviado sozinho.",
                  wraplength=520, justify="left").pack(anchor="w")
        self._list = tk.Listbox(frame, height=7, width=20, activestyle="none", exportselection=False,
                                font=theme.F_BODY, background=theme.BG_INPUT,
                                foreground=theme.TEXT_PRIMARY, selectbackground=theme.ACCENT,
                                selectforeground=theme.ON_ACCENT, highlightthickness=1,
                                highlightbackground=theme.BORDER_SUBTLE)
        self._list.pack(fill="both", expand=True, pady=theme.SPACE_3)
        fields = ttk.Frame(frame)
        fields.pack(fill="x", pady=(0, 12))
        ttk.Label(fields, text="Nome do atalho").pack(anchor="w")
        self.label = ttk.Entry(fields)
        self.label.pack(fill="x", pady=(4, 8))
        ttk.Label(fields, text="Texto para o compositor").pack(anchor="w")
        self.draft = ttk.Entry(fields)
        self.draft.pack(fill="x", pady=4)
        self._list.bind("<<ListboxSelect>>", self._select)
        row = ttk.Frame(frame)
        row.pack(fill="x")
        for index, (label, command) in enumerate((("Adicionar", self._add), ("Renomear", self._rename),
                               ("Subir", lambda: self._move(-1)), ("Descer", lambda: self._move(1)),
                               ("Remover", self._remove), ("Restaurar padrão", self._restore))):
            ttk.Button(row, text=label, command=command).grid(row=index // 3, column=index % 3,
                                                            sticky="ew", padx=4, pady=4)
        for column in range(3):
            row.columnconfigure(column, weight=1)
        footer = ttk.Frame(frame)
        footer.pack(fill="x", pady=(theme.SPACE_4, 0))
        self._status = ttk.Label(footer, text="", style="Muted.TLabel")
        self._status.pack(side="left")
        ttk.Button(footer, text="Cancelar", command=self.destroy).pack(side="right",
                                                                       padx=(theme.SPACE_2, 0))
        ttk.Button(footer, text="Salvar", command=self._save).pack(side="right")
        self._refresh()

    # ------------------------------------------------------------------ linhas --
    def _refresh(self, selected: int = 0) -> None:
        self._list.delete(0, "end")
        for item in self._items:
            self._list.insert("end", f"{item.label}  ·  {item.draft}")
        if self._items:
            selected = min(max(0, selected), len(self._items) - 1)
            self._list.selection_clear(0, "end")
            self._list.selection_set(selected)
            self._select()
        else:
            self.label.delete(0, "end")
            self.draft.delete(0, "end")

    def _index(self) -> int | None:
        selection = self._list.curselection()
        return int(selection[0]) if selection else None

    def _select(self, _event: tk.Event | None = None) -> None:
        index = self._index()
        if index is None:
            return
        current = self._items[index]
        for field, text in ((self.label, current.label), (self.draft, current.draft)):
            field.delete(0, "end")
            field.insert(0, text)

    def _entry(self) -> ui_preferences.QuickAction | None:
        label, draft = self.label.get().strip(), self.draft.get()
        if not label or not draft.strip():
            self._status.configure(text="Informe o nome e o texto do atalho.")
            return None
        return ui_preferences.QuickAction(label[:ui_preferences.LABEL_MAX],
                                          draft[:ui_preferences.DRAFT_MAX])

    def _add(self) -> None:
        if len(self._items) >= ui_preferences.MAX_ACTIONS:
            self._status.configure(text=f"Máximo de {ui_preferences.MAX_ACTIONS} atalhos.")
            return
        item = self._entry()
        if item is not None:
            self._items.append(item)
            self._refresh()

    def _rename(self) -> None:
        index = self._index()
        item = self._entry()
        if index is not None and item is not None:
            self._items[index] = item
            self._refresh(index)

    def _move(self, step: int) -> None:
        index = self._index()
        if index is None:
            return
        target = index + step
        if not 0 <= target < len(self._items):
            return
        self._items[index], self._items[target] = self._items[target], self._items[index]
        self._refresh(target)

    def _remove(self) -> None:
        index = self._index()
        if index is None:
            return
        del self._items[index]
        self._refresh(index)

    def _restore(self) -> None:
        self._items = list(ui_preferences.DEFAULT_ACTIONS)
        self._refresh()

    def _save(self) -> None:
        if not self._items:
            self._status.configure(text="Sem atalho nenhum: nada foi salvo.")
            return
        self._on_save(tuple(self._items))
        self.destroy()
