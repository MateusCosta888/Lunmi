from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
import queue
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from app import settings as settings_module
from app.brain import registry
from app.brain.contracts import BrainError, Capability
from app.brain.discovery import DiscoveryStatus, ModelListing, get_discovery
from app.brain.probe import PROBE_TIMEOUT_SECONDS, ProbeResult, probe_settings
from app.brain.registry import resolve
from app.core.controller import TaskController, TaskResult
from app.settings import Settings, save_app_selection, save_provider, save_selection
from app.ui import panels, shell, theme
from app.ui import ui_preferences
from app.ui.composer import Composer
from app.ui.transcript import Transcript
from app.ui.screen import Screen
from app.ui import window_chrome

# O teste do modelo gasta uma chamada real de inferência; dizer isso antes de o botão
# ser clicado faz parte do contrato — a verificação livre continua separada e gratuita.
PROBE_HINT = ("Teste real do modelo escolhido: gasta uma chamada mínima e mostra se ele "
              "responde e a latência medida.")

# Toda tag de `memory_call` que muda o que existe no banco. O painel direito descarta a leitura
# anterior quando uma delas responde — inclusive quando a resposta chega depois de um wipe.
MUTATING_MEMORY_TAGS = frozenset({"proposed", "accepted", "rejected", "updated", "deleted",
                                  "options", "imported", "wiped"})


class ChatWindow:
    # Rotas da barra lateral. As três primeiras têm capacidade real hoje; as duas últimas não, e
    # dizem o motivo em vez de abrir tela com dado inventado (spec §4.2 e §11.4).
    ROUTES = (("chat", "Chat", "chat"), ("memory", "Memória", "memory"),
              ("computer", "Computador", "computer"), ("projects", "Projetos", "projects"),
              ("settings", "Configurações", "settings"))
    UNAVAILABLE = {
        "computer": "a página de computer-use ainda não existe; o estado e o resultado da tarefa "
                    "estão no rodapé e no histórico desta janela",
        "projects": "não há registro de projetos no produto; nada é inferido a partir de uma "
                    "pasta encontrada",
    }

    def __init__(self, root: tk.Tk, settings: Settings) -> None:
        self.root, self.settings = root, settings
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.worker = threading.Thread(target=self._worker, name="task-worker", daemon=True)
        self.worker.start()
        self.controller = TaskController(settings, lambda state: self.events.put(("state", state)),
                                        self._confirm, self._choose, self._indicate)
        self.connection_future: concurrent.futures.Future[Any] | None = None
        # A troca de cérebro roda `controller.reconfigure` no laço do worker — que fecha o Brain e
        # o motor em voo. Enquanto ela não termina, a janela não tem um cérebro para servir um
        # despacho, e isso é uma trava própria, separada da tarefa.
        self.config_future: concurrent.futures.Future[Any] | None = None
        self._task_busy = False
        # A2/A3: one process-wide model catalogue, so reopening the dialog does not
        # re-spawn every CLI. The future is the in-flight listing, never awaited by Tk.
        self.discovery_future: concurrent.futures.Future[Any] | None = None
        self.discovery = get_discovery()
        self.model_listing: ModelListing | None = None
        # The in-flight inference probe (the "Testar modelo" button), never awaited by Tk.
        self.probe_future: concurrent.futures.Future[ProbeResult] | None = None
        # Maps a model label shown in the UI back to the provider's own id.
        self.model_labels: dict[str, str] = {}
        # FASE 3.1: the optional AppAgent brain has its own picker, catalogue and
        # in-flight listing, so choosing it never disturbs the main brain's.
        self.app_model_labels: dict[str, str] = {}
        self.app_discovery_future: concurrent.futures.Future[Any] | None = None
        self.pending_confirmation: asyncio.Future[bool] | None = None
        # F-AL4/F-AL6: the two questions the launcher may ask, answered in dialogs.
        self.pending_choice: asyncio.Future[int | None] | None = None
        self.pending_location: asyncio.Future[str] | None = None
        self.future: concurrent.futures.Future[TaskResult] | None = None
        self.closing = False
        self._prefs_window: Screen | None = None
        self._screens: dict[str, Screen] = {}
        self._question: Screen | None = None
        # MEMORY-UI: a janela de memória e as chamadas em voo vivem aqui. A UI nunca toca o
        # SQLite — cada operação sai pelo laço do worker num executor e volta pela mesma fila
        # de eventos que já transporta estado, resultado e confirmação.
        self.memory_window: Any = None
        self._memory_futures: list[concurrent.futures.Future[Any]] = []
        root.title("Lunmi")
        root.geometry(f"{theme.DEFAULT_WIDTH}x{theme.DEFAULT_HEIGHT}")
        root.minsize(theme.MIN_WIDTH, theme.MIN_HEIGHT)
        # Escala é a única leitura de arquivo que acontece **antes** de qualquer widget: o
        # `tk scaling` tem de estar certo antes da primeira fonte ser criada, e adiar para depois
        # faria a janela nascer numa escala e viver em outra. É um JSON de alguns kilobytes no
        # diretório do app; toda gravação continua fora da thread Tk.
        self.prefs_path = ui_preferences.path_for(Path(settings_module.ROOT))
        self.prefs = ui_preferences.load(self.prefs_path)
        if self.prefs.scale_pct != 100:
            root.tk.call("tk", "scaling", 96.0 * (self.prefs.scale_pct / 100.0) / 72.0)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.configure(bg=theme.BG_WINDOW)
        self.style = theme.apply(root)
        self.shell = shell.Shell(root, on_navigate=self.navigate)
        self.shell.pack(fill="both", expand=True)
        for key, label, icon in self.ROUTES:
            self.shell.add_route(key, label, icon)
        self.shell.set_active("chat")
        topbar = self.shell.topbar
        add_topbar = self.shell.add_topbar
        # A pílula mostra provider e modelo REAIS. "Lunmi Pro" da referência é texto de conceito:
        # não autoriza inventar plano nem modelo.
        self.brain_pill = add_topbar(shell.BrainButton(
            topbar, text=self._brain_label(), command=self.configure, name="brain_pill"),
            padx=theme.SPACE_2)
        self.config_button = self.brain_pill
        self.auto_approve = tk.BooleanVar(value=settings.auto_approve)
        self.approval_state = tk.StringVar(value=self._approval_label())
        self.approval_toggle = add_topbar(
            shell.Toggle(topbar, label="Autoaprovação", name="auto_approve",
                         background=theme.BG_CHAT, command=self._toggle_approval_switch),
            secondary=True, padx=theme.SPACE_2)
        self.approval_toggle.set_value(bool(settings.auto_approve))
        # Modo Conversar desligado por padrão: executar a instrução pelo agente continua sendo o
        # que acontece quando ninguém toca nisso. É controle nativo e rótulo testado.
        # Estado e Conversar são `secondary`: descem para a segunda linha quando a barra não dá
        # conta — medido em 680 px, numa linha só o rótulo ficava com 91 dos 200 px que pede.
        self.conversation = tk.BooleanVar(value=False)
        mode = ttk.Frame(topbar)
        self.conversation_toggle = ttk.Radiobutton(
            mode, name="conversation_toggle", text="Conversar", value=True,
            variable=self.conversation, style="Lunmi.Mode.TRadiobutton")
        self.conversation_toggle.pack(side="left")
        self.execution_toggle = ttk.Radiobutton(
            mode, text="Executar", value=False, variable=self.conversation,
            style="Lunmi.Mode.TRadiobutton")
        self.execution_toggle.pack(side="left", padx=(4, 0))
        add_topbar(mode, secondary=True, padx=theme.SPACE_2)
        self.memory_button = self.shell._rows["memory"]
        # Sem isto o painel direito só apareceria em janela larga: em 980 ou 680 px ele existiria
        # e não haveria como abri-lo. O botão aparece justamente quando a janela não é larga.
        def open_panels() -> None:
            self.shell.toggle_right()

        self.panels_toggle = self.shell.set_panels_toggle(open_panels)
        self.history = Transcript(self.shell.content)
        self.history.pack(fill="both", expand=True, padx=theme.SPACE_4)
        self._build_composer(self.shell.dock)
        self.panels = panels.RightPanel(
            self.shell.right, on_quick_action=self.set_draft, on_open_memory=self.open_memory,
            memory_call=self.memory_call, snapshot=self.memory_snapshot,
            on_details=self.show_details, actions=self.prefs.actions,
            on_actions=self.save_actions, on_edit=self.open_actions)
        self.append("Sistema", "Bem-vindo à Lunmi. Escreva uma instrução para começar.\n"
                    "Conversar responde sem executar ações no Windows. Ações sensíveis pedem "
                    "confirmação. A instrução e as observações UIA são enviadas ao Brain escolhido.")
        self.input.focus_set()
        restored = ui_preferences.usable_rect(self.prefs, self.root.winfo_screenwidth(),
                                              self.root.winfo_screenheight())
        if restored:
            # Só volta para onde foi guardado se ainda existir tela ali: com o monitor removido,
            # restaurar em x=2600 deixaria a janela fora do alcance do mouse.
            x, y, width, height = restored
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        self._prefs_job: str | None = None
        self._prefs_lock = threading.Lock()
        self._prefs_version = 0
        self._prefs_warned = False
        self._prefs_thread: threading.Thread | None = None
        self.root.after_idle(self._apply_chrome)
        self.root.bind("<Configure>", self._note_geometry, add="+")
        self.root.after(40, self._drain)
        if settings.prewarm:
            # FASE 8: the first command must not pay for a boot the window could have paid
            # for while the user was still reading it. Best effort only — every task starts
            # the worker anyway, so a prewarm that fails changes nothing.
            self.root.after(60, self._prewarm)

    # ------------------------------------------------------------------- visual --
    def _brain_label(self) -> str:
        """Provider e modelo verdadeiros, sem nome de fornecedor inventado na interface."""
        try:
            label = registry.get(self.settings.provider).label
        except (BrainError, KeyError):
            label = self.settings.provider
        return f"{label} · {self.settings.model}" if self.settings.model else label

    def _toggle_approval_switch(self, enabled: bool) -> None:
        """O toggle desenhado escreve na mesma `BooleanVar` que o teste manipula direto."""
        self.auto_approve.set(enabled)
        self.toggle_approval()

    def _apply_chrome(self) -> None:
        self.chrome_result = window_chrome.apply(self.root)

    def _page(self, key: str, title: str) -> Screen:
        if self._question is not None and self._question.winfo_exists() and key != "question":
            return self._question
        existing = self._screens.get(key)
        if existing is None or not existing.winfo_exists():
            existing = Screen(self.shell.content, title, self._show_chat)
            self._screens[key] = existing
            existing.bind("<Destroy>", lambda event: self._page_destroyed(key, event), add="+")
        self.history.pack_forget()
        for page in self._screens.values():
            if page.winfo_exists():
                page.pack_forget()
        existing.pack(fill="both", expand=True)
        self._composer.grid_remove()
        self.shell.set_screen_mode(True)
        self.shell.set_active(key if key in {"memory", "settings"} else "settings")
        return existing

    def _page_destroyed(self, key: str, event: tk.Event) -> None:
        page = self._screens.get(key)
        if event.widget is not page:
            return
        self._screens.pop(key, None)
        if page is self._question:
            self._question = None
        if not self.closing:
            self._show_chat()

    def _show_chat(self) -> None:
        if self._question is not None and self._question.winfo_exists():
            return
        for page in self._screens.values():
            if page.winfo_exists():
                page.pack_forget()
        self.history.pack(fill="both", expand=True, padx=theme.SPACE_4)
        self._composer.grid()
        self.shell.set_screen_mode(False)
        self.shell.set_active("chat")

    def navigate(self, key: str) -> None:
        if self._question is not None and self._question.winfo_exists():
            return
        reason = self.UNAVAILABLE.get(key)
        if reason:
            self._show_chat()
            self.append("Sistema", f"{key.capitalize()} indisponível: {reason}.")
        elif key == "chat":
            self._show_chat()
        elif key == "memory":
            self.open_memory()
        elif key == "settings":
            self.open_preferences()

    def open_preferences(self) -> None:
        if self.closing:
            return
        dialog = self._page("settings", "Configurações")
        if dialog is self._question or dialog is self._prefs_window:
            return
        self._prefs_window = dialog
        form = dialog.body
        ttk.Label(form, text="Preferências", font=theme.F_SECTION).pack(anchor="w")
        ttk.Label(form, text="A aparência mantém a identidade navy da Lunmi.",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 20))
        ttk.Label(form, text="Escala da interface · aplicada na próxima abertura").pack(anchor="w")
        selected = tk.StringVar(value=str(self.prefs.scale_pct))
        ttk.Combobox(form, textvariable=selected,
                     values=tuple(str(value) for value in ui_preferences.SCALE_CHOICES),
                     state="readonly", width=10).pack(anchor="w", pady=8)
        ttk.Label(form, text="A posição e o tamanho da janela são salvos automaticamente.",
                  style="Muted.TLabel", wraplength=450).pack(anchor="w", pady=(8, 20))
        buttons = ttk.Frame(form)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Configurar AI Brain", command=self.configure).pack(side="left")
        ttk.Button(buttons, text="Memória", command=self.open_memory).pack(side="left", padx=8)
        def dismiss() -> None:
            self._prefs_window = None
            dialog.destroy()
        dialog.back.configure(command=dismiss)
        def save() -> None:
            self._write_prefs(replace(self.prefs, scale_pct=int(selected.get())))
            dismiss()
        ttk.Button(form, text="Salvar preferências", command=save).pack(anchor="e", pady=(20, 0))

    def _build_composer(self, dock: tk.Frame) -> None:
        """Faixa inferior: estado à esquerda, caixa arredondada com campo e Enviar, Parar à direita.

        A caixa é uma `theme.Surface` — fundo arredondado com um contêiner dentro. Campo e botão
        continuam widgets nativos: seleção, foco e teclado são do toolkit, não pintados.

        `fit=True` é o que faz a moldura acompanhar o campo: multilinha significa que a altura do
        compositor muda a cada linha digitada, e qualquer altura fixa decidida aqui estaria errada
        na linha seguinte. Foi assim que a versão de uma linha cortou o botão em 2 px: a conta
        vivia num número e o controle não sabia dela.
        """
        row = tk.Frame(dock, background=theme.BG_CHAT)
        row.pack(fill="x", pady=(theme.SPACE_3, theme.SPACE_2))
        self.dock_row = row
        # `grid`, não `pack`: com a caixa em `pack(side="left", expand=True)` ela come a cavidade
        # inteira e o que é empacotado depois sai da janela — medido em 700x560, onde "Ready"
        # aparecia cortado e "Parar" simplesmente sumia. No grid a caixa leva o resto, depois de
        # estado e Parar terem o tamanho próprio.
        row.columnconfigure(0, weight=1)
        box = theme.Surface(row, fill=theme.BG_INPUT, outline=theme.BORDER_SUBTLE,
                            radius=26, background=theme.BG_CHAT,
                            fit="height", padding=(theme.SPACE_4, theme.SPACE_2),
                            name="composer")
        box.grid(row=0, column=0, columnspan=3, sticky="ew")
        self._composer = box
        self.input = Composer(box.inner, on_submit=self.submit)
        self.input.pack(side="left", fill="x", expand=True)
        size = theme.to_physical(self.root, 18)
        self._send_icon = tk.PhotoImage(master=self.root, width=size, height=size)
        ink = theme.ON_ACCENT
        center = size // 2
        for y in range(size // 4, size * 3 // 4):
            self._send_icon.put(ink, to=(center - 1, y))
            self._send_icon.put(ink, to=(center, y))
        for offset in range(size // 4 + 1):
            for x in (center - offset, center + offset):
                self._send_icon.put(ink, to=(x, size // 4 + offset))
        self.send_button = ttk.Button(box.inner, text="Enviar", image=self._send_icon,
                                      compound="none", command=self.submit,
                                      style="Lunmi.Send.TButton", padding=(10, 10))
        # Ancorado embaixo: com o campo em seis linhas, o botão centralizado ficaria boiando no
        # meio da caixa de texto.
        self.send_button.pack(side="right", anchor="se", padx=(theme.SPACE_3, 0))
        box.bind("<Button-1>", lambda _: self.input.focus_set())
        # Estado e Parar na mesma linha da caixa, e criados aqui porque é a geometria desta linha
        # que decide a ordem: no grid eles recebem o tamanho próprio antes de a caixa pegar o resto.
        self.state = tk.StringVar(value="Ready")
        self.state_label = ttk.Label(row, textvariable=self.state, font=theme.F_SMALL)
        self.state_label.grid(row=1, column=1, pady=(theme.SPACE_2, 0), padx=theme.SPACE_3)
        self.approval_state_label = ttk.Label(row, name="approval_state",
                                             textvariable=self.approval_state,
                                             background=theme.BG_CHAT,
                                             font=theme.F_SMALL, style="Muted.TLabel")
        self.approval_state_label.grid(row=1, column=0, sticky="w",
                                       pady=(theme.SPACE_2, 0), padx=theme.SPACE_3)
        self.state.trace_add("write", self._paint_state)
        self.stop_button = ttk.Button(row, text="Parar", command=self.stop, state="disabled")
        self.stop_button.grid(row=1, column=2, pady=(theme.SPACE_2, 0))

    def _paint_state(self, *_: object) -> None:
        """Estado por palavra e cor. A cor nunca é a única pista — a palavra continua lá."""
        color = {"Ready": theme.TEXT_MUTED, "Thinking": theme.ACCENT,
                 "Executing": theme.ACCENT, "Verifying": theme.ACCENT,
                 "Completed": theme.SUCCESS, "Failed": theme.ERROR,
                 "Cancelled": theme.TEXT_MUTED}.get(self.state.get(), theme.TEXT_MUTED)
        try:
            self.state_label.configure(foreground=color)
        except tk.TclError:
            pass

    def _approval_label(self) -> str:
        return "Modo: Autoaprovação" if self.settings.auto_approve else "Modo: Manual"

    def toggle_approval(self) -> None:
        """The user's mode, saved and sent to the runtime; a running task keeps its own.

        What changes is the question the runtime would have asked because it could not
        tell whether an action was safe. What does not change is the authorisation for
        actions that are provably sensitive — auto-approval is not a blanket permission.
        """
        enabled = bool(self.auto_approve.get())
        self.controller.set_auto_approve(enabled)
        self.settings = replace(self.settings, auto_approve=enabled)
        self.approval_state.set(self._approval_label())
        self.append("Sistema", (
            "Autoaprovação ligada: ações normais (digitar, pesquisar, navegar, abrir, "
            "selecionar, atalhos comuns) e as que não puderem ser classificadas são "
            "executadas sem perguntar. Ações sabidamente sensíveis (excluir, sobrescrever, "
            "salvar, fechar, enviar/publicar, alterar configurações) continuam pedindo "
            "autorização. Vale a partir da próxima tarefa.") if enabled else (
            "Autoaprovação desligada: o modo Manual volta a pedir autorização para tudo "
            "que não for comprovadamente normal. Vale a partir da próxima tarefa."))

    def _prewarm(self) -> None:
        asyncio.run_coroutine_threadsafe(self._warm_engine(), self.loop)

    async def _warm_engine(self) -> None:
        # F-AL1: the applications this machine has are indexed while the user is still
        # reading the window — structured sources first (about a second), then the sweep,
        # in a thread that nobody waits for. A failure changes nothing: a name that is not
        # in the index is searched, and a name that is not found goes to the agent.
        self.controller.catalog.start()
        try:
            await self.controller.engine.start()
        except Exception as exc:  # noqa: BLE001 - a prewarm that fails is not a failure
            logging.warning("Preaquecimento do worker UFO não concluiu: %s", exc)

    async def _confirm(self, message: str) -> bool:
        future: asyncio.Future[bool] = self.loop.create_future()
        self.pending_confirmation = future
        self.events.put(("confirm", (message, future)))
        try:
            return await future
        finally:
            self.pending_confirmation = None

    async def _choose(self, label: str, candidates: list[dict[str, Any]]) -> int | None:
        """Ask which application a name meant; ``None`` means "none of these" (F-AL4)."""
        future: asyncio.Future[int | None] = self.loop.create_future()
        self.pending_choice = future
        self.events.put(("choose", (label, candidates, future)))
        try:
            return await future
        finally:
            self.pending_choice = None

    async def _indicate(self, query: str) -> str:
        """Ask where a program is that nothing on this machine knows (F-AL6)."""
        future: asyncio.Future[str] = self.loop.create_future()
        self.pending_location = future
        self.events.put(("locate", (query, future)))
        try:
            return await future
        finally:
            self.pending_location = None

    def _worker(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def append(self, speaker: str, text: str) -> None:
        """Um turno no transcurso. A assinatura é contrato; a apresentação é do `Transcript`."""
        self.history.append(speaker, text)

    def history_text(self) -> str:
        """O texto exibido nos cartões atuais, na ordem visual.

        Substitui o `history.get("1.0","end")` do `Text` único. Lê os widgets, não um cache: é
        assim que a checagem de que uma credencial não apareceu na tela continua valendo por
        aquilo que está na tela.
        """
        return self.history.text()

    def show_result(self, result: TaskResult) -> None:
        """Cartão do turno, com os campos que o runtime realmente mediu.

        Cada número vem do `TaskResult`; linha sem dado correspondente não é escrita — um zero no
        lugar de uma fase que não aconteceu diria que ela aconteceu e custou zero. `false_success`
        tem linha própria: o veredito do agente não é a prova da janela.

        `memory_error` não vai cru para a tela pelo mesmo motivo de `_memory_reply`: mensagem de
        driver pode carregar o texto que o usuário tentou gravar.
        """
        origin = {"deterministic": "determinístico (sem LLM)",
                  "deterministic+llm": "determinístico declinado → agente",
                  "llm": "agente"}.get(result.plan_source, "")
        measures = [
            (f"Total {result.duration_ms:.0f} ms · LLM {result.llm_time_ms:.0f} ms · "
             f"UI {result.observation_time_ms:.0f} ms · Ação {result.execution_time_ms:.0f} ms · "
             f"Verificação {result.verification_time_ms:.0f} ms · "
             f"Inicialização {result.startup_time_ms:.0f} ms"),
            (f"LLM {result.llm_calls} chamadas · {result.actions} ações "
             f"({result.actions_executed} executadas) · {result.steps} passos"),
        ]
        context = [f"plano: {origin}" if origin else "", f"escopo: {result.scope}"]
        if result.total_tokens is not None:
            context.append(f"tokens {result.input_tokens or 0} entrada / "
                           f"{result.output_tokens or 0} saída · {result.total_tokens} total")
        line = " · ".join(item for item in context if item)
        if line != "escopo: end_to_end":
            measures.append(line)
        # F-AL7: the application resolution, reported separately from the launch it triggered —
        # which source answered, how long the name took, and how the program was actually opened.
        if result.app_index_hit or result.app_launch_kind:
            detail = []
            if result.app_source:
                detail.append(f"índice {result.app_source}")
            if result.app_launch_kind:
                detail.append(result.app_launch_kind)
            detail.append(f"resolução {result.app_resolve_ms:.0f} ms")
            if result.app_search_ms:
                detail.append(f"busca {result.app_search_ms:.0f} ms")
            if result.app_candidates:
                detail.append(f"{result.app_candidates} candidatos")
            if result.app_index_cold:
                detail.append("índice frio")
            measures.append("app: " + ", ".join(detail))
        if result.approval_mode:
            asked = sum(1 for item in result.approvals if item.get("asked"))
            measures.append(f"aprovação: {result.approval_mode} "
                            f"({len(result.approvals) - asked} automáticas, {asked} perguntas)")
        if result.failure_kind:
            measures.append(f"falha: {result.failure_kind}")
        if result.false_success:
            measures.append("SUCCESS sem evidência anexada: nada observado confirma o resultado")
        if result.memory_error:
            measures.append("memória: indisponível nesta tarefa")
        self.history.result(result.status, result.message, measures)
        self.panels.record(result)
        if result.scope != "conversation":
            # Uma tarefa pode ter escrito episódio/proposta no banco. O painel é atualizado pelo
            # mesmo caminho das operações de memória, não por suposição: leitura nova, fora do Tk.
            self.panels.invalidate()

    def _note_geometry(self, event: tk.Event) -> None:
        """Redimensionar empilha eventos; grava-se uma vez, 600 ms depois do último."""
        if self.closing or event.widget is not self.root:
            return
        pending = getattr(self, "_prefs_job", None)
        if pending is not None:
            self.root.after_cancel(pending)
        self._prefs_job = self.root.after(600, self._persist_geometry)

    def _persist_geometry(self, *, force: bool = False) -> None:
        self._prefs_job = None
        if self.closing and not force:
            return
        try:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            width, height = self.root.winfo_width(), self.root.winfo_height()
        except tk.TclError:
            return
        if width < ui_preferences.MIN_WINDOW_EDGE or height < ui_preferences.MIN_WINDOW_EDGE:
            return
        prefs = ui_preferences.Preferences(window=(x, y, width, height),
                                           scale_pct=self.prefs.scale_pct,
                                           actions=self.prefs.actions)
        self._write_prefs(prefs)

    def open_actions(self) -> None:
        page = self._page("actions", "Ações rápidas")
        if page is self._question or page.body.winfo_children():
            return
        editor = panels.ActionEditor(page.body, self.panels._actions, self.panels._editor_saved)
        editor.pack(fill="both", expand=True)
        editor.bind("<Destroy>", lambda event: page.destroy()
                    if event.widget is editor and page.winfo_exists() else None)
        page.bind_scroll()

    def save_actions(self, actions: tuple[ui_preferences.QuickAction, ...]) -> None:
        """O editor gravou: o painel já mudou, e o arquivo é escrito fora da thread Tk."""
        self._write_prefs(ui_preferences.with_actions(self.prefs, actions))

    def _write_prefs(self, prefs: ui_preferences.Preferences) -> None:
        self.prefs = prefs
        path = self.prefs_path
        self._prefs_version += 1
        version = self._prefs_version

        def worker() -> None:
            with self._prefs_lock:
                if version != self._prefs_version:
                    return
                saved = ui_preferences.save(path, prefs)
            if not saved:
                self.events.put(("prefs_failed", None))

        self._prefs_thread = threading.Thread(target=worker, name="prefs-write", daemon=True)
        self._prefs_thread.start()

    def set_draft(self, text: str) -> None:
        """Ação rápida acrescenta ao compositor e foca no fim. Não envia, não apaga.

        Apagar o que estava lá era perda de dados: o campo tem `undo=False` de propósito (cartão de
        leitura não desfaz, e um campo de rascunho com histórico de desfazer dividiria estado com a
        tarefa), então um `delete(0, "end")` aqui seria irreversível. Com texto, o modelo entra no
        fim, separado; sem texto, entra limpo.
        """
        current = self.input.get()
        if current and not current.endswith((" ", "\n")):
            text = " " + text
        self.input.insert("end", text)
        self.input.focus_set()
        self.input.mark_set("insert", "end-1c")
        self.input.see("insert")

    def show_details(self, result: TaskResult) -> None:
        """Reexibe um `TaskResult` já guardado nesta sessão. Não reexecuta nada.

        Só o que o contrato tem: veredito, frase, tempos, plano, escopo, contagens e o estado da
        evidência como **contagem** — payloads de evidência podem carregar caminho e texto da
        máquina, e o painel não é lugar de despejar isso no histórico.
        """
        evidence = result.evidence
        verified = sum(1 for item in evidence if item.get("verified"))
        self.append("Sistema", (
            f"Reexibindo resultado desta sessão ({result.status}): {result.message}\n"
            f"Total {result.duration_ms:.0f} ms · LLM {result.llm_time_ms:.0f} ms · "
            f"Ação {result.execution_time_ms:.0f} ms · "
            f"Verificação {result.verification_time_ms:.0f} ms · {result.steps} passos\n"
            f"escopo {result.scope} · evidência {len(evidence)} registros ({verified} verificados)"
            + (f" · falha {result.failure_kind}" if result.failure_kind else "")))

    def memory_snapshot(self) -> dict[str, Any]:
        """Uma leitura de memória, chamada fora da thread Tk.

        A flag `enabled` é lida **antes** de qualquer listagem (B3): `MemoryManager.list` não
        verifica a flag, e um painel que ligasse nela reapresentaria o perfil de quem desligou a
        memória. Só kind e texto aparecem; identificador, timestamps e caminho não vêm para cá.
        """
        manager = getattr(self.controller, "memory", None)
        if manager is None:
            return {"available": False, "enabled": False, "items": []}
        flags = manager.options()
        if not flags.get("enabled"):
            return {"available": True, "enabled": False, "items": []}
        items = [(item.kind, item.text) for item in manager.list(limit=panels.MEMORY_LIMIT)]
        return {"available": True, "enabled": True, "items": items}

    def _busy(self, busy: bool) -> None:
        """Trava ou libera a UI para uma **tarefa** em voo.

        A trava real é recomposta em `_refresh_locks()`, porque duas coisas diferentes podem estar
        em voo ao mesmo tempo (tarefa e troca de cérebro), e cada uma tranca um conjunto próprio.
        Um booleano só não dá: foi assim que um `configured` tardio liberava a janela inteira
        enquanto a tarefa ainda rodava.
        """
        self._task_busy = busy
        self._refresh_locks()

    def _refresh_locks(self) -> None:
        reconfiguring = self.config_future is not None and not self.config_future.done()
        busy = bool(self._task_busy) or reconfiguring
        self.send_button.configure(state="disabled" if busy else "normal")
        self.config_button.configure(state="disabled" if busy else "normal")
        self.memory_button.set_enabled(not busy)
        self.conversation_toggle.configure(state="disabled" if busy else "normal")
        self.execution_toggle.configure(state="disabled" if busy else "normal")
        # Parar pertence à tarefa, não à configuração: habilitá-lo durante um `reconfigure`
        # ofereceria cancelar algo que não existe, e desabilitá-lo quando a tarefa continua em voo
        # tiraria do usuário o único controle que ainda vale.
        self.stop_button.configure(state="normal" if self._task_busy else "disabled")


    def submit(self) -> None:
        if self.closing or self.future and not self.future.done():
            return
        if self.config_future is not None and not self.config_future.done():
            # Trocar de cérebro fecha o worker em voo: despachar agora entregaria a instrução a uma
            # janela que ainda não tem Brain novo. O rascunho fica intacto — quem digitou não
            # pediu para ele desaparecer.
            return
        instruction = self.input.get().strip()
        if not instruction:
            return
        self.input.delete(0, "end")
        self.append("Você", instruction)
        self._busy(True)
        talk = bool(self.conversation.get())
        if talk:
            # Dito antes de rodar, para ninguém descobrir depois que uma "conversa" mexeu no
            # desktop. O modo conversa não executa ferramentas.
            self.append("Sistema", "Modo Conversar: nenhuma ação no Windows será executada nesta "
                        "resposta.")
        self.future = asyncio.run_coroutine_threadsafe(
            self.controller.run(instruction, conversation=talk), self.loop)
        self.future.add_done_callback(lambda future: self.events.put(("result", future)))

    def stop(self) -> None:
        # Cancel the asyncio task directly so its cleanup completes before we
        # enable another submission; Future.cancel would mark done too early.
        self.loop.call_soon_threadsafe(self.controller.cancel)
        self.stop_button.configure(state="disabled")

    # ------------------------------------------------------------------ memória --
    def open_memory(self) -> None:
        """Abre (ou traz para frente) a tela de memória. Uma só por vez."""
        if self.future and not self.future.done():
            return
        page = self._page("memory", "Memória")
        if page is self._question:
            return
        window = self.memory_window
        if window is not None and not window.closing and window.winfo_exists():
            return
        from app.ui.memory_window import MemoryWindow
        self.memory_window = MemoryWindow(self, parent=page.body)
        self.memory_window.pack(fill="both", expand=True)
        self.memory_window.load_options()
        page.bind_scroll()

    def memory_attach(self, window: Any) -> None:
        """A janela se registra/desregistra; chamadas em voo continuam válidas por payload."""
        if window is None and self.memory_window is window:
            self.memory_window = None
        elif window is not None:
            self.memory_window = window

    def memory_call(self, tag: str, function: Any, *args: Any,
                    on_ok: Any = None, sink: Any = None, **kwargs: Any) -> None:
        """Roda uma chamada síncrona de memória fora da thread Tk e devolve pelo fila.

        O executor pertence ao laço do worker: a thread Tk não espera, não faz ``join`` e não
        acessa o SQLite. O resultado volta como evento ``memory`` e é aplicado na thread Tk.

        ``sink`` é o destino vivo da resposta (B2). Sem ele, o destino é a janela de memória — o
        painel direito tem o seu, e uma resposta que chega depois de o painel ter deixado de
        existir não toca Tcl destruído.
        """
        window = sink if sink is not None else self.memory_window
        if self.closing:
            return
        if getattr(self.controller, "memory", None) is None:
            if window is not None and not window.closing and window.winfo_exists():
                window.set_status("Memória indisponível: o backend ainda não foi carregado.")
            return

        async def run() -> Any:
            return await self.loop.run_in_executor(None, functools.partial(function, *args,
                                                                           **kwargs))
        try:
            future = asyncio.run_coroutine_threadsafe(run(), self.loop)
        except RuntimeError:
            if window is not None and not window.closing and window.winfo_exists():
                window.set_status("Falha: o laço de tarefas não está disponível.")
            return
        self._memory_futures.append(future)
        future.add_done_callback(lambda done: self.events.put(("memory", (tag, done, on_ok,
                                                                         window))))

    def _memory_reply(self, tag: str, done: concurrent.futures.Future[Any], on_ok: Any,
                      window: Any) -> None:
        """Aplica o resultado de uma operação de memória. Chamado só pela thread Tk."""
        self._memory_futures = [item for item in self._memory_futures if not item.done()]
        alive = window is not None and not window.closing and window.winfo_exists()
        mutating = tag in MUTATING_MEMORY_TAGS
        try:
            result = done.result()
        except concurrent.futures.CancelledError:
            return
        except Exception as exc:
            # Não se ecoa str(exc) cru: mensagem de driver SQLite pode carregar o texto que o
            # usuário tentou gravar, e esse texto pode ser sensível.
            if alive:
                window.set_status(f"Falha ao gravar ou ler a memória ({type(exc).__name__}).")
            else:
                self.append("Sistema", f"Operação de memória falhou ({type(exc).__name__}).")
            return
        if on_ok is not None and alive:
            on_ok(result)
        if mutating:
            # Wipe, apagar, desligar, gravar: a leitura do painel deixou de valer antes mesmo de o
            # chamador saber se deu certo. Invalidar aqui é o que impede uma resposta atrasada de
            # reapresentar o perfil apagado.
            self.panels.invalidate()

    def _drain(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "state":
                self.state.set(value)
            elif kind == "result":
                try:
                    self.show_result(value.result())
                except Exception:
                    self.append("Sistema", "Falha interna; consulte logs/app.log.")
                    self.state.set("Failed")
                self._busy(False)
            elif kind == "confirm":
                message, approval = value
                if self.closing or approval.done():
                    continue
                self._show_confirmation(message, approval)
            elif kind == "choose":
                label, candidates, answer = value
                if self.closing or answer.done():
                    continue
                self._show_choice(label, candidates, answer)
            elif kind == "locate":
                query, answer = value
                if self.closing or answer.done():
                    continue
                self._show_location(query, answer)
            elif kind == "connection":
                future, window, status, button, callback = value
                if not window.winfo_exists():
                    continue
                try:
                    result = future.result()
                    status.set(callback(result) if callback else result)
                except concurrent.futures.CancelledError:
                    status.set("Verificação cancelada.")
                except Exception as exc:
                    status.set(str(exc))
                button.configure(state="normal")
            elif kind == "probe":
                future, window, status, button = value
                if not window.winfo_exists():
                    continue
                button.configure(state="normal")
                try:
                    measured = future.result()
                except concurrent.futures.CancelledError:
                    continue  # trocou de provider no meio: o texto atual já vale mais
                except Exception as exc:
                    status.set(f"Falha no teste do modelo: {type(exc).__name__}.")
                    continue
                status.set(measured.summary())
            elif kind == "models":
                future, window, provider_id, status, render = value
                if not window.winfo_exists():
                    continue
                try:
                    listing = future.result()
                except concurrent.futures.CancelledError:
                    continue
                except Exception as exc:
                    status.set(f"Falha na descoberta de modelos: {type(exc).__name__}.")
                    continue
                message = render(provider_id, listing)
                if message:
                    status.set(message)
            elif kind == "app_models":
                future, window, provider_id, status, render = value
                if not window.winfo_exists():
                    continue
                try:
                    listing = future.result()
                except concurrent.futures.CancelledError:
                    continue
                except Exception as exc:
                    status.set(f"Falha na descoberta de modelos: {type(exc).__name__}.")
                    continue
                message = render(provider_id, listing)
                if message:
                    status.set(message)
            elif kind == "configured":
                future, window, settings = value
                self.config_future = None
                try:
                    future.result()
                    save_selection(settings.provider, settings.model)
                    self.settings = settings
                    # A pílula mostra provider e modelo: se ela não acompanha o apply, a barra de
                    # topo passa a mentir sobre o cérebro que está em uso. Só aqui, depois do
                    # resultado bem-sucedido — falha de configuração não muda a tela.
                    self.brain_pill.set_text(self._brain_label())
                    try:
                        # O usuário pôde fechar o diálogo enquanto o apply rodava; destruir de novo
                        # levanta TclError, e mostrá-lo como "falha de configuração" mentiria sobre
                        # um apply que deu certo.
                        window.destroy()
                    except tk.TclError:
                        pass
                    app_brain = (f" · AppAgent {settings.app_provider} / {settings.app_model}"
                                 if settings.app_provider else " · AppAgent usa o Brain principal")
                    self.append("Sistema", f"Cérebro configurado: {settings.provider} / {settings.model}."
                                f"{app_brain} "
                                "Ao enviar, sua instrução será transmitida a esse provider.")
                except Exception as exc:
                    message = f"Não foi possível aplicar o Brain. Erro: {type(exc).__name__}."
                    self.append("Sistema", message)
                    if window.winfo_exists():
                        ttk.Label(window.body, text=message, wraplength=440).pack(
                            anchor="w", pady=8)
                    else:
                        self._show_chat()
                # Não é `_busy(False)`: se havia tarefa em voo, ela continua em voo, e liberar a
                # janela aqui desabilitaria o "Parar" que ainda vale.
                self._refresh_locks()
            elif kind == "memory":
                tag, done, on_ok, window = value
                self._memory_reply(tag, done, on_ok, window)
            elif kind == "prefs_failed" and not self._prefs_warned:
                # Uma vez só: preferência que não grava não é motivo para encher o histórico,
                # mas também não é para calar — a próxima abertura vai mostrar o padrão.
                self._prefs_warned = True
                self.append("Sistema", "Não foi possível gravar as preferências de tela; elas "
                                        "valem até fechar a janela.")
        if not self.closing:
            self.root.after(40, self._drain)

    def _show_confirmation(self, message: str, approval: asyncio.Future[bool]) -> None:
        # Nonblocking, no grab: Stop remains usable while awaiting consent.
        window = self._page("question", "Confirmar ação de risco")
        self._question = window
        frame = ttk.Frame(window.body, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=message, wraplength=560, justify="left").pack(fill="x")
        def respond(allowed: bool) -> None:
            def resolve() -> None:
                if not approval.done():
                    approval.set_result(allowed)
            self.loop.call_soon_threadsafe(resolve)
            window.destroy()
        ttk.Button(frame, text="Autorizar esta ação", command=lambda: respond(True)).pack(side="right", pady=12)
        decline = ttk.Button(frame, text="Não", command=lambda: respond(False))
        decline.pack(side="right", padx=10, pady=12)
        decline.focus_set()
        window.back.configure(command=lambda: respond(False))
        window.bind_scroll()
        def poll() -> None:
            if not window.winfo_exists():
                return
            if approval.done() or self.closing:
                window.destroy()
                return
            self.root.after(40, poll)
        poll()

    def _watch(self, window: Screen, answer: asyncio.Future[Any]) -> None:
        """Close a dialog whose answer already arrived (Stop, or a reply from elsewhere)."""
        if not window.winfo_exists():
            return
        if answer.done() or self.closing:
            window.destroy()
            return
        self.root.after(40, lambda: self._watch(window, answer))

    def _show_choice(self, label: str, candidates: list[dict[str, Any]],
                     answer: asyncio.Future[int | None]) -> None:
        """Let the user pick between genuinely different applications (F-AL4).

        The list shows what tells two installs apart — the publisher and the path — because
        the names are the same by definition. The choice is remembered by the index, so the
        same phrase never asks twice.
        """
        window = self._page("question", "Qual aplicativo?")
        self._question = window
        frame = ttk.Frame(window.body, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"'{label}' pode ser mais de um aplicativo. Qual deles?",
                  wraplength=560, justify="left").pack(fill="x")
        listing = tk.Listbox(frame, height=min(6, max(2, len(candidates))), width=20,
                             font=theme.F_BODY, bg=theme.BG_INPUT, fg=theme.TEXT_PRIMARY)
        for item in candidates:
            where = str(item.get("install_dir") or item.get("target") or "")
            publisher = str(item.get("publisher") or "")
            suffix = "  —  ".join(piece for piece in (publisher, where) if piece)
            listing.insert("end", f"{item.get('name')}{'  ·  ' + suffix if suffix else ''}")
        listing.selection_set(0)
        listing.pack(fill="both", expand=True, pady=10)

        def respond(position: int | None) -> None:
            def resolve() -> None:
                if not answer.done():
                    answer.set_result(position)
            self.loop.call_soon_threadsafe(resolve)
            window.destroy()

        chosen = lambda: int(listing.curselection()[0]) if listing.curselection() else 0
        ttk.Button(frame, text="Abrir esta aplicação",
                   command=lambda: respond(chosen())).pack(side="right", pady=8)
        decline = ttk.Button(frame, text="Nenhuma (não abrir)", command=lambda: respond(None))
        decline.pack(side="right", padx=10, pady=8)
        decline.focus_set()
        listing.bind("<Double-Button-1>", lambda _: respond(chosen()))
        window.back.configure(command=lambda: respond(None))
        window.bind_scroll()
        self._watch(window, answer)

    def _show_location(self, query: str, answer: asyncio.Future[str]) -> None:
        """Offer to point at a program nothing on this machine knows (F-AL6).

        Declining is easy and explicit: "deixar o agente resolver" sends the instruction to
        the HostAgent exactly as it would have gone without the index, so a question that
        the user does not want to answer never blocks the task.
        """
        window = self._page("question", "Onde está o aplicativo?")
        self._question = window
        frame = ttk.Frame(window.body, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=(f"Nenhum aplicativo instalado respondeu por '{query}'.\n\n"
                              "Indique a pasta onde ele fica ou o próprio executável e, a partir daí, "
                              "o agente abre sozinho — inclusive nas próximas vezes."),
                  wraplength=560, justify="left").pack(fill="x")

        def respond(value: str) -> None:
            def resolve() -> None:
                if not answer.done():
                    answer.set_result(value)
            self.loop.call_soon_threadsafe(resolve)
            window.destroy()

        def pick(kind: str) -> None:
            if kind == "folder":
                chosen = filedialog.askdirectory(title=f"Pasta de {query}", parent=window)
            else:
                chosen = filedialog.askopenfilename(title=f"Executável de {query}", parent=window,
                                                   filetypes=[("Aplicativo", "*.exe")])
            respond(str(chosen or ""))

        ttk.Button(frame, text="Indicar pasta…",
                   command=lambda: pick("folder")).pack(side="right", pady=10)
        ttk.Button(frame, text="Indicar executável…",
                   command=lambda: pick("file")).pack(side="right", padx=8, pady=10)
        agent = ttk.Button(frame, text="Deixar o agente resolver", command=lambda: respond(""))
        agent.pack(side="left", pady=10)
        agent.focus_set()
        window.back.configure(command=lambda: respond(""))
        window.bind_scroll()
        # A question about a location has a safe default: unanswered, the agent takes over.
        timeout = self.root.after(45_000, lambda: respond("") if window.winfo_exists() else None)
        def cancel_timeout(event: tk.Event) -> None:
            if event.widget is window:
                self.root.after_cancel(timeout)
        window.bind("<Destroy>", cancel_timeout, add="+")
        self._watch(window, answer)

    def configure(self) -> None:
        """Brain picker generated from the registry: the UI has no vendor knowledge."""
        if self.future and not self.future.done():
            return
        window = self._page("brain", "AI Brain")
        if window is self._question or window.body.winfo_children():
            return
        frame = ttk.Frame(window.body)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        available = registry.descriptors()
        by_label = {descriptor.label: descriptor for descriptor in available}
        current = registry.get(self.settings.provider)
        choice = tk.StringVar(value=current.label)
        model = tk.StringVar(value=self.settings.model)
        key = tk.StringVar(value=self.settings.api_key)
        url = tk.StringVar(value=self.settings.base_url)
        remember = tk.BooleanVar(value=False)
        status = tk.StringVar(value="Escolha um cérebro e clique em verificar.")
        probe_status = tk.StringVar(value=PROBE_HINT)

        ttk.Label(frame, text="AI Brain").grid(row=0, column=0, sticky="w", pady=5)
        # Widgets are named so tests and the UI can address them without geometry.
        picker = ttk.Combobox(frame, name="brain_picker", textvariable=choice,
                              values=[item.label for item in available], state="readonly", width=22)
        picker.grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Modelo").grid(row=1, column=0, sticky="w", pady=5)
        model_picker = ttk.Combobox(frame, name="model_picker", textvariable=model, width=22)
        model_picker.grid(row=1, column=1, sticky="ew", pady=5)
        refresh = ttk.Button(frame, name="refresh_models", text="Atualizar lista",
                             command=lambda: request_models(force=True))
        refresh.grid(row=1, column=2, padx=(8, 0), pady=5)
        key_label = ttk.Label(frame, name="api_key_label", text="Chave API")
        key_entry = ttk.Entry(frame, name="api_key", textvariable=key, show="●", width=22)
        key_label.grid(row=2, column=0, sticky="w", pady=5)
        key_entry.grid(row=2, column=1, sticky="ew", pady=5)
        url_label = ttk.Label(frame, name="base_url_label", text="URL base")
        url_entry = ttk.Entry(frame, name="base_url", textvariable=url, width=22)
        url_label.grid(row=3, column=0, sticky="w", pady=5)
        url_entry.grid(row=3, column=1, sticky="ew", pady=5)
        remember_box = ttk.Checkbutton(frame, name="remember_credentials", text="Salvar credenciais", variable=remember)
        remember_box.grid(row=4, columnspan=2, sticky="w", pady=10)
        ttk.Label(frame, text="Cérebro/modelo são lembrados ao aplicar; chave e URL somente com consentimento.",
                  wraplength=450).grid(row=5, columnspan=2, sticky="w")
        ttk.Label(frame, name="status_label", textvariable=status, wraplength=450,
                  justify="left").grid(row=6, columnspan=2, sticky="w", pady=8)
        ttk.Label(frame, name="probe_status_label", textvariable=probe_status, wraplength=520,
                  justify="left").grid(row=8, columnspan=3, sticky="w", pady=(0, 6))

        # FASE 3.1: "Brain do AppAgent (opcional)". The HostAgent keeps the reasoning brain
        # above; a fast model can drive the AppAgent's execution rounds without changing
        # anything else. Empty means "reuse the main brain", which is the old behaviour.
        ttk.Separator(frame, orient="horizontal").grid(row=10, columnspan=3, sticky="ew", pady=(10, 4))
        app_enabled = tk.BooleanVar(value=bool(self.settings.app_provider or self.settings.app_model))
        try:
            app_current = registry.get(self.settings.app_provider) if self.settings.app_provider else current
        except KeyError:
            # A provider id the registry no longer knows (a hand-edited file) must not
            # break the whole dialog: the section opens on the main brain's provider.
            app_current = current
        app_choice = tk.StringVar(value=app_current.label)
        app_model = tk.StringVar(value=self.settings.app_model)
        app_status = tk.StringVar(value="")
        ttk.Label(frame, text="Brain do AppAgent (opcional)").grid(row=11, column=0, sticky="w")
        app_toggle = ttk.Checkbutton(frame, name="app_brain_toggle",
                                     text="Usar um modelo separado (rápido) para o AppAgent",
                                     variable=app_enabled, command=lambda: update_app_controls())
        app_toggle.grid(row=12, columnspan=3, sticky="w")
        ttk.Label(frame, text="Provider (AppAgent)").grid(row=13, column=0, sticky="w", pady=5)
        app_picker = ttk.Combobox(frame, name="app_brain_picker", textvariable=app_choice,
                                  values=[item.label for item in available], state="disabled", width=22)
        app_picker.grid(row=13, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Modelo (AppAgent)").grid(row=14, column=0, sticky="w", pady=5)
        app_model_picker = ttk.Combobox(frame, name="app_model_picker", textvariable=app_model,
                                        width=22, state="disabled")
        app_model_picker.grid(row=14, column=1, sticky="ew", pady=5)
        app_refresh = ttk.Button(frame, name="app_refresh_models", text="Atualizar lista",
                                 state="disabled", command=lambda: request_app_models(force=True))
        app_refresh.grid(row=14, column=2, padx=(8, 0), pady=5)
        ttk.Label(frame, name="app_status_label", textvariable=app_status, wraplength=520,
                  justify="left").grid(row=15, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(frame, text="LLM_APP_PROVIDER / LLM_APP_MODEL no ambiente têm prioridade "
                              "sobre esta escolha.", wraplength=520,
                  justify="left").grid(row=16, columnspan=3, sticky="w")

        def descriptor() -> registry.BrainDescriptor:
            return by_label[choice.get()]

        def candidate() -> Settings:
            chosen = descriptor()
            return replace(
                self.settings, provider=chosen.id, model=model.get().strip(),
                api_key=key.get().strip() if chosen.requires_api_key else "",
                base_url=url.get().strip() if chosen.location is registry.BrainLocation.LOCAL_ENDPOINT else "",
            )

        def selection() -> Settings:
            """O candidato do diálogo, com o rótulo do modelo traduzido para o id do provider.

            O combobox mostra rótulos; inferir e verificar exigem o id que o provider
            conhece — é ele que diz se o modelo testado é o mesmo da listagem. O Brain do
            AppAgent entra da mesma forma; desligado, ele é limpo (o campo vazio significa
            "usa o Brain principal").
            """
            settings = candidate()
            if settings.model in self.model_labels:
                settings = replace(settings, model=self.model_labels[settings.model])
            if app_enabled.get():
                chosen = app_descriptor()
                app_model_id = app_model.get().strip()
                if app_model_id in self.app_model_labels:
                    app_model_id = self.app_model_labels[app_model_id]
                settings = replace(settings, app_provider=chosen.id, app_model=app_model_id)
            else:
                settings = replace(settings, app_provider="", app_model="")
            return settings

        discovery = self.discovery

        def render_models(provider_id: str, listing: ModelListing) -> str:
            """Show one catalogue; report the saved selection instead of rewriting it.

            Returns the line to display — the caller owns the status widget. An existing
            choice is never swapped for another model behind the user's back; a model is
            filled in only when there is none yet, and then by the provider's own default
            or its curated recommendation, not by whichever row it prints first.
            """
            if descriptor().id != provider_id:
                return ""
            self.model_listing = listing
            previous = model.get().strip()
            self.model_labels = {item.label or item.id: item.id for item in listing.models}
            model_picker.configure(values=[item.label or item.id for item in listing.models])
            # A real catalogue turns the field into a list. Typing an id the provider
            # does not recognise is exactly what used to fail later; manual entry stays
            # available only where there is no catalogue to choose from.
            model_picker.configure(state="readonly" if listing.models else "normal")
            if not listing.ok:
                return listing.detail
            by_id = {item.id: (item.label or item.id) for item in listing.models}
            if not previous:
                preferred = next((item for item in listing.models if item.default), listing.models[0])
                model.set(preferred.label or preferred.id)
            elif previous in by_id:
                model.set(by_id[previous])
            return " ".join(piece for piece in (listing.detail, listing.selection_problem(previous))
                            if piece)

        async def load_models(target: Settings, *, force: bool) -> ModelListing:
            return await discovery.list(target.provider, settings=target, force=force)

        def request_models(*, force: bool) -> None:
            """Ask the selected provider for its models without ever blocking the dialog."""
            chosen = descriptor()
            try:
                target = candidate()
                # The model itself is not needed to list models, so it is the one
                # requirement relaxed here; provider, key and URL are still enforced.
                target.validate(model=False)
            except Exception as exc:
                status.set(str(exc))
                return
            pending = self.discovery_future
            if pending is not None and not pending.done():
                if not force:
                    return
                pending.cancel()
            status.set(f"Carregando modelos de {chosen.label}…")
            model_picker.configure(state="disabled")
            self.discovery_future = asyncio.run_coroutine_threadsafe(
                load_models(target, force=force), self.loop)
            self.discovery_future.add_done_callback(lambda done: self.events.put(
                ("models", (done, window, chosen.id, status, render_models))))

        def update_controls(*_: Any) -> None:
            chosen = descriptor()
            # Show only the fields this provider actually uses.
            if chosen.requires_api_key:
                key_label.grid()
                key_entry.grid()
            else:
                key_label.grid_remove()
                key_entry.grid_remove()
            if chosen.location is registry.BrainLocation.LOCAL_ENDPOINT:
                url_label.grid()
                url_entry.grid()
            else:
                url_label.grid_remove()
                url_entry.grid_remove()
            model_picker.configure(values=())
            self.model_labels = {}
            self.model_listing = None
            # O veredito anterior era de outro cérebro: trocar de provider o invalida.
            probe_status.set(PROBE_HINT)
            if self.probe_future is not None and not self.probe_future.done():
                self.probe_future.cancel()
            probe_button.configure(state="normal")
            if chosen.kind is registry.BrainKind.AGENT_RUNTIME:
                status.set(f"{chosen.label}: usa o login do próprio CLI — nenhuma chave é pedida aqui.")
                remember.set(False)
                remember_box.grid_remove()
                interactive = chosen.capabilities.supports(Capability.INTERACTIVE_LOGIN)
                test_button.configure(text="Conectar conta" if interactive else "Verificar CLI")
            else:
                status.set("A verificação consulta o endpoint sem gastar tokens de inferência.")
                remember_box.grid()
                remember_box.configure(text="Salvar chave/URL neste Windows (DPAPI)")
                test_button.configure(text="Verificar conexão")

        def on_provider_change(*_: Any) -> None:
            """Choosing a provider is enough: its models load by themselves (A2/A3)."""
            update_controls()
            request_models(force=False)

        picker.bind("<<ComboboxSelected>>", on_provider_change)

        def app_descriptor() -> registry.BrainDescriptor:
            return by_label[app_choice.get()]

        def app_target() -> Settings:
            """The settings that carry the AppAgent brain's own provider.

            The same provider as the main brain reuses its credential and endpoint; a
            different one resolves its own environment credential and never receives the
            main provider's secret — the same rule ``app_brain_settings`` enforces.
            """
            chosen = app_descriptor()
            if chosen.id == self.settings.provider:
                return replace(self.settings, provider=chosen.id)
            from app.settings import API_KEY_PROVIDERS
            key_name = chosen.id.upper() + "_API_KEY" if chosen.id in API_KEY_PROVIDERS else "LLM_APP_API_KEY"
            return replace(self.settings, provider=chosen.id, model="",
                           api_key=os.getenv(key_name, ""), base_url=os.getenv("LLM_APP_BASE_URL", ""))

        def render_app_models(provider_id: str, listing: ModelListing) -> str:
            """Show the AppAgent provider's catalogue without touching the main picker."""
            if app_descriptor().id != provider_id:
                return ""
            previous = app_model.get().strip()
            self.app_model_labels = {item.label or item.id: item.id for item in listing.models}
            app_model_picker.configure(values=[item.label or item.id for item in listing.models])
            # A real catalogue turns the field into a list; manual entry stays available
            # only where the provider lists nothing (e.g. a CLI without model discovery).
            app_model_picker.configure(state="readonly" if listing.models else "normal")
            if not listing.ok:
                return listing.detail
            by_id = {item.id: (item.label or item.id) for item in listing.models}
            if not previous:
                preferred = next((item for item in listing.models if item.default), listing.models[0])
                app_model.set(preferred.label or preferred.id)
            elif previous in by_id:
                app_model.set(by_id[previous])
            return " ".join(piece for piece in (listing.detail, listing.selection_problem(previous))
                            if piece)

        def request_app_models(*, force: bool) -> None:
            """Ask the AppAgent provider for its models without blocking the dialog."""
            chosen = app_descriptor()
            try:
                target = app_target()
                # The model itself is not needed to list models; provider, key and URL
                # still are, exactly as on the main picker.
                target.validate(model=False)
            except Exception as exc:
                # No catalogue can be fetched here (missing key/URL for that provider),
                # but the model id is still the user's to type: manual entry stays open.
                app_status.set(str(exc))
                app_model_picker.configure(state="normal")
                return
            pending = self.app_discovery_future
            if pending is not None and not pending.done():
                if not force:
                    return
                pending.cancel()
            app_status.set(f"Carregando modelos de {chosen.label}…")
            app_model_picker.configure(state="disabled")
            self.app_discovery_future = asyncio.run_coroutine_threadsafe(
                load_models(target, force=force), self.loop)
            self.app_discovery_future.add_done_callback(lambda done: self.events.put(
                ("app_models", (done, window, chosen.id, app_status, render_app_models))))

        def update_app_controls(*_: Any) -> None:
            enabled = bool(app_enabled.get())
            app_picker.configure(state="readonly" if enabled else "disabled")
            app_refresh.configure(state="normal" if enabled else "disabled")
            category = "normal" if not app_model_picker.cget("values") else "readonly"
            app_model_picker.configure(state=category if enabled else "disabled")
            if not enabled:
                app_status.set("Sem Brain separado: o AppAgent usa o Brain principal.")
                return
            app_status.set("")
            request_app_models(force=False)

        app_picker.bind("<<ComboboxSelected>>", update_app_controls)

        async def probe(settings: Settings, capabilities: Any) -> tuple[str, ModelListing]:
            # Everything Tk is read before entering the loop thread; only the
            # brain is touched here.
            brain = resolve(settings)
            try:
                health = await brain.health()
                listing = await discovery.list(settings.provider, settings=settings, brain=brain,
                                               force=True)
                if not listing.ok and capabilities.supports(Capability.INTERACTIVE_LOGIN):
                    # Only the explicit button may open a vendor login; nothing here is
                    # automatic. A provider that lists nothing may still offer its
                    # catalogue once the account is connected (the Codex app-server does).
                    login = getattr(brain, "login", None)
                    if callable(login):
                        try:
                            entries = tuple(await login())
                        except BrainError:
                            entries = ()
                        if entries:
                            listing = discovery.store(ModelListing(
                                settings.provider, listing.label, DiscoveryStatus.READY, models=entries,
                                detail=f"{len(entries)} modelos disponíveis."))
                return f"{health.status.value}: {health.detail}", listing
            finally:
                await brain.close()

        def show(result: tuple[str, ModelListing]) -> str:
            message, listing = result
            extra = render_models(listing.provider, listing)
            return " ".join(piece for piece in (message, extra) if piece)

        def verify() -> None:
            try:
                settings = selection()
                settings.validate()
            except Exception as exc:
                status.set(str(exc))
                return
            if self.connection_future and not self.connection_future.done():
                return
            chosen = descriptor()
            test_button.configure(state="disabled")
            status.set("Verificando…")
            self.connection_future = asyncio.run_coroutine_threadsafe(
                probe(settings, chosen.capabilities), self.loop)
            self.connection_future.add_done_callback(lambda done: self.events.put(
                ("connection", (done, window, status, test_button, show))))

        def run_probe() -> None:
            """Uma inferência real no modelo escolhido, medida — para qualquer provedor.

            A verificação ao lado é grátis e não prova que *este* modelo responde; a
            sonda prova, gastando uma chamada mínima. Ela roda no laço do app, então a
            janela continua respondendo enquanto o provider pensa.
            """
            chosen = descriptor()
            try:
                settings = selection()
                settings.validate()
            except Exception as exc:
                probe_status.set(str(exc))
                return
            pending = self.probe_future
            if pending is not None and not pending.done():
                return
            probe_button.configure(state="disabled")
            probe_status.set(f"Testando {settings.model or chosen.label}…")
            self.probe_future = asyncio.run_coroutine_threadsafe(
                probe_settings(settings, timeout=min(PROBE_TIMEOUT_SECONDS, settings.timeout_seconds)),
                self.loop)
            self.probe_future.add_done_callback(lambda done: self.events.put(
                ("probe", (done, window, probe_status, probe_button))))

        def apply() -> None:
            if self._task_busy:
                # B5: `controller.reconfigure` fecha o Brain e o motor. Com tarefa em voo isso não é
                # "aplicar depois" — é derrubar o trabalho no meio. O diálogo continua abrindo para
                # leitura; o que não acontece é a troca, e o motivo aparece onde o botão está.
                status.set("Há uma tarefa em voo. Pare a tarefa antes de aplicar: trocar de cérebro "
                           "fecha o worker que está executando agora.")
                return
            try:
                settings = selection()
                settings.validate()
                # Non-secret pair; an empty provider removes the file, so clearing the
                # toggle really falls back to the main brain on the next launch.
                save_app_selection(settings.app_provider, settings.app_model)
                if remember.get():
                    save_provider(settings.provider, settings.model, settings.api_key, settings.base_url)
                if self.connection_future and not self.connection_future.done():
                    self.connection_future.cancel()
                if self.config_future is not None and not self.config_future.done():
                    return          # um apply por vez: o primeiro ainda está trocando o cérebro
                future = asyncio.run_coroutine_threadsafe(self.controller.reconfigure(settings), self.loop)
                # Fechar o diálogo não desfaz um apply: cancelar no meio deixaria o controller sem
                # Brain velho (já fechado) e sem Brain novo. A trava fica até o `configured`.
                self.config_future = future
                self._refresh_locks()
                future.add_done_callback(lambda done: self.events.put(("configured", (done, window, settings))))
            except Exception as exc:
                messagebox.showerror("Configuração", str(exc), parent=window)

        def dismiss() -> None:
            if self.connection_future and not self.connection_future.done():
                self.connection_future.cancel()
            if self.discovery_future and not self.discovery_future.done():
                self.discovery_future.cancel()
            if self.app_discovery_future and not self.app_discovery_future.done():
                self.app_discovery_future.cancel()
            if self.probe_future and not self.probe_future.done():
                self.probe_future.cancel()
            window.destroy()

        test_button = ttk.Button(frame, name="verify_button", text="Verificar", command=verify)
        test_button.grid(row=7, column=0, sticky="w", pady=8)
        probe_button = ttk.Button(frame, name="probe_button", text="Testar modelo", command=run_probe)
        probe_button.grid(row=7, column=1, sticky="w", padx=(8, 0), pady=8)
        ttk.Button(frame, name="apply_button", text="Aplicar", command=apply).grid(row=7, column=2, sticky="e", pady=8)
        ttk.Button(frame, text="Cancelar", command=dismiss).grid(row=9, column=0, sticky="w", pady=8)
        window.back.configure(command=dismiss)
        window.bind_scroll()
        # Opening the dialog discovers the current provider right away, so an invalid
        # saved model is visible before anything is applied. No login is ever started
        # here: that stays behind the explicit button.
        update_controls()
        request_models(force=False)
        update_app_controls()

    def close(self) -> None:
        if self.closing:
            return
        if self._prefs_job is not None:
            self.root.after_cancel(self._prefs_job)
            self._prefs_job = None
        self._persist_geometry(force=True)
        self.closing = True
        if self._prefs_window is not None and self._prefs_window.winfo_exists():
            self._prefs_window.destroy()
        self._prefs_window = None
        if self.connection_future and not self.connection_future.done():
            self.connection_future.cancel()
        if self.discovery_future and not self.discovery_future.done():
            self.discovery_future.cancel()
        if self.app_discovery_future and not self.app_discovery_future.done():
            self.app_discovery_future.cancel()
        if self.probe_future and not self.probe_future.done():
            self.probe_future.cancel()
        # A janela fecha antes do resto: quem encerra o SQLite é o controller, não a UI — fechar
        # o store daqui competiria com uma tarefa em voo.
        window = self.memory_window
        if window is not None and not window.closing and window.winfo_exists():
            window.destroy()
        self.memory_window = None
        for pending in self._memory_futures:
            if not pending.done():
                pending.cancel()
        self._memory_futures = []
        self._busy(True)
        self.state.set("Cancelled")
        future = asyncio.run_coroutine_threadsafe(self.controller.close(), self.loop)

        def poll() -> None:
            if not future.done() or (self._prefs_thread is not None
                                     and self._prefs_thread.is_alive()):
                self.root.after(40, poll)
                return
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.root.destroy()
        poll()
