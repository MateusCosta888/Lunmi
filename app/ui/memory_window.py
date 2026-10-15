"""Janela funcional de memoria do AgentApp — M1 + M2.

Escrita por Design. Reaproveita ttk existente: nenhum visual novo, nenhuma migração de stack,
nenhum orb. O papel desta tela e dar ao usuario controle sobre o que o app lembra.

Regras que este arquivo segue de propósito:

- Nunca fala com SQLite nem faz I/O de arquivo na thread Tk. Toda leitura, escrita e exportação
  passa por ``host.memory_call``, que roda fora da thread Tk e devolve o resultado pela fila de
  eventos já existente. A importação recusa arquivo acima de ``MAX_IMPORT_BYTES`` antes de abrir.
- Não cria botão com o texto exato "Não" nem "Autorizar esta ação": os smoke de confirmação
  localizam o diálogo de risco por esses rótulos atravessando os ``Toplevel`` filhos da raiz, e
  esta janela também é um deles. Confirmações destrutivas usam ``messagebox`` nativo, com foco
  inicial na resposta negativa.
- Não exibe segredo nem credencial: mostra apenas kind, texto, status, fonte e datas. ``metadata``
  não é renderizado, e erro mostra só o nome da exceção — mensagem de driver SQLite pode carregar
  o texto que o usuário tentou gravar.
- Adicionar conteúdo passa por proposta (aceite explícito), não por gravação direta no perfil.
- Consulta de episódios usa a API pública: ``recall(query, kinds=("episode",), limit=...)``.
"""
from __future__ import annotations

import functools
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from app.memory import MemoryItem
from app.memory.contracts import PROFILE_KINDS

# Teto de importação. Um arquivo maior é recusado antes de abrir o handle: ler JSON gigante na
# UI congela a janela e estoura a memória do processo.
MAX_IMPORT_BYTES = 4 * 1024 * 1024

# Colunas exibidas. "id" fica escondido de propósito: é identificador interno, não informação
# útil para quem revisa a memória, e ocuparia a linha inteira.
_COLUMNS = ("kind", "text", "status", "source", "updated")
_WIDTH = {"kind": 92, "text": 220, "status": 84, "source": 108, "updated": 132}

_OPTIONS = (("enabled", "Memória ativa"), ("episodes", "Guardar episódios de tarefa"),
            ("proposals", "Gerar propostas"), ("history", "Histórico de conversa"))

_KINDS = PROFILE_KINDS

# Episódios são um kind público (app.memory.contracts.KINDS inclui "episode"), então a consulta
# usa a API do plano: recall com filtro de kind. Não existe nem é criada uma API paralela.
_EPISODE_KIND = "episode"
_EPISODE_LIMIT = 100


class MemoryWindow(ttk.Frame):
    """Superfície de memória: listar, buscar, editar, apagar, exportar, importar, limpar,
    opções, propostas e — quando o backend expuser — episódios."""

    def __init__(self, host: Any, *, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.host = host
        self.closing = False
        self._rows: list[Any] = []
        self._proposal_rows: list[Any] = []
        self._pending_flags: dict[str, bool] = {}
        self._options_loaded = False

        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        root.rowconfigure(5, weight=1)

        # ---- busca ---------------------------------------------------------------
        bar = ttk.Frame(root)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Buscar").grid(row=0, column=0, sticky="w")
        self.search = ttk.Entry(bar, name="memory_search")
        self.search.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self.search.bind("<Return>", lambda _: self.reload())
        ttk.Button(bar, name="memory_reload", text="Atualizar", command=self.reload).grid(
            row=0, column=2)
        # O tipo é escolha do usuário: gravar um fato como "preferência" contamina o que o
        # assistente vai injetar como perfil permanente.
        self.propose_kind = ttk.Combobox(bar, name="memory_propose_kind", values=list(_KINDS),
                                         state="readonly", width=12)
        self.propose_kind.set(_KINDS[0])
        self.propose_kind.grid(row=1, column=0, sticky="w", pady=(6, 0))
        # A proposta tem caixa própria: a caixa de edição da lista fica desabilitada quando nada
        # está selecionado, e com o banco vazio não haveria onde escrever a primeira memória.
        self.propose_text = ttk.Entry(bar, name="memory_propose_text")
        self.propose_text.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(bar, name="memory_propose", text="Nova proposta",
                   command=self._propose_new).grid(row=1, column=2, columnspan=3,
                                                   sticky="w", padx=(6, 0), pady=(6, 0))

        # ---- lista de memórias ---------------------------------------------------
        list_frame = ttk.Frame(root)
        list_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        list_frame.columnconfigure(0, weight=1)
        self.listbox = ttk.Treeview(list_frame, columns=_COLUMNS, displaycolumns=("kind", "text", "status"), show="headings", height=8,
                                    selectmode="browse")
        for key in _COLUMNS:
            self.listbox.heading(key, text={"kind": "Tipo", "text": "Texto", "status": "Estado",
                                            "source": "Origem",
                                            "updated": "Atualizado em"}.get(key, key))
            self.listbox.column(key, width=_WIDTH[key], stretch=key == "text")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.bind("<<TreeviewSelect>>", lambda _: self._sync_edit_enabled())
        self.listbox.bind("<Configure>", self._fit_columns)

        actions = ttk.Frame(root)
        actions.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(actions, name="memory_delete", text="Apagar selecionada",
                   command=self._delete).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Label(actions, text="Editar texto", style="TLabel").grid(row=1, column=0, sticky="w")
        self.edit_text = ttk.Entry(actions, name="memory_edit_text", state="disabled")
        self.edit_text.grid(row=2, column=0, sticky="ew", padx=(0, 6))
        actions.columnconfigure(0, weight=1)
        self.edit_save = ttk.Button(actions, name="memory_update", text="Salvar",
                                    command=self._update, state="disabled")
        self.edit_save.grid(row=2, column=1)

        ttk.Separator(root, orient="horizontal").grid(row=4, column=0, sticky="ew", pady=10)

        # ---- propostas -----------------------------------------------------------
        prop_box = ttk.LabelFrame(root, text="Propostas (não entram no perfil sem aceite)")
        prop_box.grid(row=5, column=0, sticky="nsew")
        prop_box.columnconfigure(0, weight=1)
        self.proposals = ttk.Treeview(prop_box, columns=("kind", "text"), show="headings",
                                      height=4, selectmode="browse")
        self.proposals.heading("kind", text="Tipo")
        self.proposals.heading("text", text="Texto proposto")
        self.proposals.column("kind", width=92, stretch=False)
        self.proposals.column("text", width=240, stretch=True)
        self.proposals.grid(row=0, column=0, sticky="nsew")
        prop_actions = ttk.Frame(prop_box)
        prop_actions.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(prop_actions, name="memory_accept", text="Aceitar proposta",
                   command=self._accept_proposal).grid(row=0, column=0, sticky="ew", padx=4)
        ttk.Button(prop_actions, name="memory_reject", text="Rejeitar proposta",
                   command=self._reject_proposal).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(prop_actions, text="Atualizar propostas",
                   command=self.reload_proposals).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        prop_actions.columnconfigure((0, 1), weight=1)

        # ---- opções e episódios --------------------------------------------------
        bottom = ttk.Frame(root)
        bottom.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(1, weight=1)

        opt_box = ttk.LabelFrame(bottom, text="Opções")
        opt_box.grid(row=0, column=0, sticky="nw")
        self.option_vars: dict[str, tk.BooleanVar] = {}
        for key, label in _OPTIONS:
            var = tk.BooleanVar(value=False)
            self.option_vars[key] = var
            ttk.Checkbutton(opt_box, name=f"memory_opt_{key}", text=label, variable=var,
                            command=functools.partial(self._flag_option, key)).grid(
                row=len(self.option_vars) - 1, column=0, sticky="w")
        self.options_save = ttk.Button(opt_box, name="memory_options_save",
                                       text="Salvar opções", command=self._save_options,
                                       state="disabled")
        self.options_save.grid(row=len(self.option_vars), column=0, sticky="w", pady=(6, 0))

        right = ttk.Frame(bottom)
        right.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        right.columnconfigure(0, weight=1)
        ttk.Button(right, name="memory_export", text="Exportar…",
                   command=self._export).grid(row=0, column=0, sticky="ew")
        ttk.Button(right, name="memory_import", text="Importar…",
                   command=self._import).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(right, name="memory_wipe", text="Limpar tudo…",
                   command=self._wipe).grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.episodes_button = ttk.Button(right, name="memory_episodes",
                                          text="Consultar episódios", command=self._episodes)
        self.episodes_button.grid(row=3, column=0, sticky="ew", pady=(6, 0))

        # ---- rodapé --------------------------------------------------------------
        # Privacidade: memória local não implica inferência privada. O texto é literal porque o
        # usuário precisa poder conferir o que foi dito ao app.
        ttk.Label(root, wraplength=420, justify="left", style="TLabel",
                  text=("Se o cérebro escolhido for um provedor de nuvem, trechos recuperados que "
                        "entram no prompt podem ser enviados a ele. Memória local não é inferência "
                        "privada. Segredos e conteúdo de terceiros não devem ser gravados.")
                  ).grid(row=7, column=0, sticky="ew", pady=(10, 0))
        self.status = ttk.Label(root, name="memory_status", text="Carregando…", wraplength=420,
                                justify="left")
        self.status.grid(row=8, column=0, sticky="ew", pady=(6, 0))

        self.bind("<Destroy>", self._on_destroy, add="+")
        host.memory_attach(self)
        self.reload()
        self.reload_proposals()

    # ------------------------------------------------------------------ helpers ---
    @property
    def memory(self) -> Any:
        """O gerenciador exposto pelo controller, ou ``None`` quando o backend ainda não existe."""
        return getattr(self.host.controller, "memory", None)

    def set_status(self, text: str) -> None:
        if self.winfo_exists():
            self.status.configure(text=text)

    def _fail(self, text: str) -> None:
        """Erro visível e específico. Nunca ecoa conteúdo que possa ser segredo do usuário."""
        self.set_status(f"Falha: {text}")

    def _selected_id(self, tree: ttk.Treeview, rows: list[Any]) -> str | None:
        """Identificador da linha selecionada, ou ``None`` com a razão dita na tela.

        Um clique que não resolve identificador não pode terminar em nada silencioso: sem mensagem,
        o usuário acha que apagou ou aceitou.
        """
        if not tree.selection():
            self.set_status("Selecione um item da lista primeiro.")
            return None
        index = tree.index(tree.selection()[0])
        if index >= len(rows):
            self.set_status("A lista mudou durante a seleção; atualize e tente de novo.")
            return None
        item_id = str(getattr(rows[index], "id", "")).strip()
        if not item_id:
            self.set_status("O item selecionado não tem identificador; o backend não o registrou.")
            return None
        return item_id

    def _fit_columns(self, event: tk.Event) -> None:
        visible = _COLUMNS if event.width >= 700 else ("kind", "text", "status")
        self.listbox.configure(displaycolumns=visible)
        fixed = sum(_WIDTH[key] for key in visible if key != "text")
        self.listbox.column("text", width=max(120, event.width - fixed - 4))

    def _sync_edit_enabled(self) -> None:
        selected = bool(self.listbox.selection())
        state = "normal" if selected else "disabled"
        self.edit_text.configure(state=state)
        self.edit_save.configure(state=state)
        if selected and self._rows:
            item = self._rows[self.listbox.index(self.listbox.selection()[0])]
            self.edit_text.configure(state="normal")
            self.edit_text.delete(0, "end")
            self.edit_text.insert(0, str(getattr(item, "text", "")))
            self.edit_text.configure(state=state)

    def _on_destroy(self, _event: tk.Event) -> None:
        if _event.widget is not self:
            return
        self.closing = True
        self.host.memory_attach(None)

    # ------------------------------------------------------------------- carga ---
    def reload(self) -> None:
        """Lista memórias ativas (ou o resultado da busca) fora da thread Tk."""
        store = self.memory
        if store is None:
            self.set_status("Memória indisponível: o backend ainda não foi carregado.")
            return
        query = self.search.get().strip()
        self.set_status("Buscando memória…")
        self.host.memory_call("list", store.list, query=query, on_ok=self._show_list)

    def _show_list(self, items: list[Any]) -> None:
        self._rows = list(items)
        self.listbox.delete(*self.listbox.get_children())
        for item in self._rows:
            self.listbox.insert("", "end", values=(
                getattr(item, "kind", ""), getattr(item, "text", ""),
                getattr(item, "status", ""), getattr(item, "source", ""),
                getattr(item, "updated_at", "")))
        self._sync_edit_enabled()
        self.set_status(f"{len(self._rows)} memória(s) ativa(s)."
                        if not self.search.get().strip()
                        else f"{len(self._rows)} resultado(s) para a busca.")

    def reload_proposals(self) -> None:
        store = self.memory
        if store is None:
            return
        self.host.memory_call("proposals", store.proposals, on_ok=self._show_proposals)

    def _show_proposals(self, items: list[Any]) -> None:
        self._proposal_rows = list(items)
        self.proposals.delete(*self.proposals.get_children())
        for item in self._proposal_rows:
            self.proposals.insert("", "end", values=(getattr(item, "kind", ""),
                                                     getattr(item, "text", "")))

    def _accept_proposal(self) -> None:
        """Aceite explícito é o único caminho de uma proposta para o perfil."""
        store = self.memory
        item_id = self._selected_id(self.proposals, self._proposal_rows)
        if store is None or item_id is None:
            return
        self.set_status("Aceitando proposta…")
        self.host.memory_call("accepted", lambda: store.accept_proposal(item_id),
                              on_ok=self._after_accepted)

    def _after_accepted(self, _id: Any) -> None:
        self.reload_proposals()
        self.reload()
        self.set_status("Proposta aceita e no perfil.")

    def _reject_proposal(self) -> None:
        store = self.memory
        item_id = self._selected_id(self.proposals, self._proposal_rows)
        if store is None or item_id is None:
            return
        self.set_status("Rejeitando proposta…")
        self.host.memory_call("rejected", lambda: store.reject_proposal(item_id),
                              on_ok=self._after_rejected)

    def _after_rejected(self, _none: Any) -> None:
        self.reload_proposals()
        self.set_status("Proposta rejeitada; nada foi gravado no perfil.")

    # ------------------------------------------------------------------ escrita --
    def _update(self) -> None:
        store = self.memory
        item_id = self._selected_id(self.listbox, self._rows)
        if store is None or item_id is None:
            return
        text = self.edit_text.get().strip()
        if not text:
            self.set_status("O texto não pode ficar vazio.")
            return
        self.set_status("Salvando…")
        self.host.memory_call("updated", lambda: store.update(item_id, text),
                              on_ok=self._after_update)

    def _after_update(self, _item: Any) -> None:
        self.reload()
        self.set_status("Memória atualizada.")

    def _delete(self) -> None:
        store = self.memory
        item_id = self._selected_id(self.listbox, self._rows)
        if store is None or item_id is None:
            return
        if not messagebox.askyesno("Apagar memória", "Apagar esta memória?", parent=self,
                                   default=messagebox.NO):
            self.set_status("Nada foi apagado.")
            return
        self.set_status("Apagando…")
        self.host.memory_call("deleted", lambda: store.delete(item_id), on_ok=self._after_delete)

    def _after_delete(self, _none: Any) -> None:
        self.reload()
        self.set_status("Memória apagada.")

    def _propose_new(self) -> None:
        """Adiciona como proposta: o aceite explícito continua sendo o que muda o perfil."""
        store = self.memory
        if store is None:
            return
        text = self.propose_text.get().strip()
        if not text:
            self.set_status("Escreva o texto da proposta no campo próprio.")
            return
        item = MemoryItem(kind=self.propose_kind.get(), text=text, source="user_explicit")
        self.set_status("Enviando proposta…")
        self.host.memory_call("proposed", lambda: store.propose(item),
                              on_ok=self._after_proposed)

    def _after_proposed(self, _id: Any) -> None:
        self.propose_text.delete(0, "end")
        self.reload_proposals()
        self.set_status("Proposta criada. Aceite-a para entrar no perfil.")

    # ------------------------------------------------------------------ opções ---
    def _flag_option(self, key: str) -> None:
        self._pending_flags[key] = bool(self.option_vars[key].get())
        self.options_save.configure(state="normal")

    def show_options(self, flags: dict[str, bool]) -> None:
        """Preenche as caixas com o que o backend devolve. Chamado pela fila, na thread Tk."""
        for key, _label in _OPTIONS:
            self.option_vars[key].set(bool(flags.get(key, False)))
        self._pending_flags = {}
        self._options_loaded = True
        self.options_save.configure(state="disabled")

    def _save_options(self) -> None:
        store = self.memory
        if store is None or not self._pending_flags:
            return
        flags = dict(self._pending_flags)
        self.set_status("Salvando opções…")
        self.host.memory_call("options", lambda: store.set_options(**flags),
                              on_ok=self._after_options)

    def _after_options(self, _none: Any) -> None:
        self._pending_flags.clear()
        self.options_save.configure(state="disabled")
        self.set_status("Opções salvas.")

    def load_options(self) -> None:
        store = self.memory
        if store is None:
            return
        self.host.memory_call("options_loaded", store.options, on_ok=self.show_options)

    # ------------------------------------------------------- exportar / importar -
    # Ler ou escrever arquivo na thread Tk congela a janela proporcionalmente ao tamanho do
    # arquivo. Os dois caminhos abaixo fazem I/O dentro de memory_call, fora da thread Tk, e a
    # importação recusa arquivo grande antes de abrir o handle.
    def _export(self) -> None:
        store = self.memory
        if store is None:
            return
        path = filedialog.asksaveasfilename(title="Exportar memória", parent=self,
                                            defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if not path:
            self.set_status("Exportação cancelada.")
            return
        if not messagebox.askyesno("Exportar memória",
                                   "Se o arquivo já existir, ele será substituído. Continuar?",
                                   parent=self, default=messagebox.NO):
            self.set_status("Exportação cancelada.")
            return
        self.set_status("Exportando…")
        self.host.memory_call("exported", lambda: self._write_export(store, path),
                              on_ok=self._after_export)

    @staticmethod
    def _write_export(store: Any, path: str) -> int:
        """Lê o dump e grava o arquivo fora da thread Tk, numa única operação."""
        text = json.dumps(store.export(), ensure_ascii=False, indent=2)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return len(text)

    def _after_export(self, characters: Any) -> None:
        self.set_status(f"Exportação concluída ({int(characters)} caracteres).")

    def _import(self) -> None:
        store = self.memory
        if store is None:
            return
        path = filedialog.askopenfilename(title="Importar memória", parent=self,
                                          filetypes=[("JSON", "*.json")])
        if not path:
            self.set_status("Importação cancelada.")
            return
        try:
            size = Path(path).stat().st_size
        except OSError:
            self._fail("não foi possível ler as informações do arquivo.")
            return
        if size > MAX_IMPORT_BYTES:
            self._fail(f"o arquivo tem {size // 1024} KiB e o limite de importação é "
                       f"{MAX_IMPORT_BYTES // (1024 * 1024)} MiB. Nada foi lido.")
            return
        self.set_status("Lendo arquivo…")
        self.host.memory_call("import_read", lambda: self._read_import(path),
                              on_ok=self._confirm_import)

    @staticmethod
    def _read_import(path: str) -> dict[str, Any]:
        """Lê e valida fora da thread Tk. O conteúdo não vai para mensagem de erro."""
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("o topo do JSON precisa ser um objeto de exportação")
        return payload

    def _confirm_import(self, payload: dict[str, Any]) -> None:
        store = self.memory
        if store is None:
            return
        if not messagebox.askyesno("Importar memória",
                                   "Importar vai acrescentar conteúdo ao que já existe. "
                                   "Continuar?", parent=self, default=messagebox.NO):
            self.set_status("Importação cancelada; nada foi alterado.")
            return
        self.set_status("Importando…")
        self.host.memory_call("imported", lambda: store.import_data(payload),
                              on_ok=self._after_import)

    def _after_import(self, count: Any) -> None:
        self.reload()
        self.set_status(f"{int(count)} item(ns) importados.")

    def _wipe(self) -> None:
        store = self.memory
        if store is None:
            return
        if not messagebox.askyesno("Limpar tudo",
                                   "Isto apaga memórias, episódios e propostas deste app. "
                                   "A ação não pode ser desfeita. Continuar?",
                                   parent=self, default=messagebox.NO, icon="warning"):
            self.set_status("Nada foi apagado.")
            return
        self.set_status("Limpando…")
        self.host.memory_call("wiped", store.wipe, on_ok=self._after_wipe)

    def _after_wipe(self, _none: Any) -> None:
        self.reload()
        self.reload_proposals()
        # ``wipe`` apaga a tabela options e reinsere DEFAULT_OPTIONS. Sem recarregar, a tela
        # continuaria marcando flags que o backend já não tem — pior: o usuário poderia salvar por
        # cima achando que está desmarcando o que já foi.
        self.load_options()
        self.set_status("Memória limpa. Opções voltaram ao padrão.")

    # ---------------------------------------------------------------- episódios --
    def _episodes(self) -> None:
        """Episódio é um kind público, então a consulta usa recall com filtro de kind."""
        store = self.memory
        if store is None:
            return
        query = self.search.get().strip()
        self.set_status("Consultando episódios…")
        self.host.memory_call("episodes",
                              lambda: store.recall(query, kinds=(_EPISODE_KIND,),
                                                   limit=_EPISODE_LIMIT),
                              on_ok=self._show_episodes)

    def _show_episodes(self, items: list[Any]) -> None:
        self._rows = list(items)
        self.listbox.delete(*self.listbox.get_children())
        for item in self._rows:
            self.listbox.insert("", "end", values=(
                getattr(item, "kind", ""), getattr(item, "text", ""),
                getattr(item, "status", ""), getattr(item, "source", ""),
                getattr(item, "updated_at", "")))
        self._sync_edit_enabled()
        self.set_status(f"{len(self._rows)} episódio(s). \"Atualizar\" volta para as memórias.")
