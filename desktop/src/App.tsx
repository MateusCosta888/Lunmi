import React, { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { getBridgeApi, makeSendCommand, type SendCommandFn } from "./shared/ipc/client";
import { usePresenceDraft } from "./shared/ipc/presence";
import { useSession } from "./shared/ipc/useSession";
import { Icon } from "./shared/ui";
import type { ApprovalDto, BrainGetDto, BrainStateDto, ChatMode, MemoryProfileDto, UiPreferencesDto } from "./shared/types";
import { SessionFailureStrip } from "./features/session/SessionState";
import { INTRO_DURATION_MS, StartupScreen } from "./features/session/StartupScreen";
import { ChatPanel } from "./features/chat/ChatPanel";
import { HistoryNavigation } from './features/chat/HistoryNavigation';
import { useChatTask } from "./features/chat/useChatTask";
import { useVoiceInput } from "./features/voice/useVoiceInput";
import { useVoiceWarmup } from "./features/voice/useVoiceWarmup";
import { useHoldController } from "./features/voice/useHoldController";
import { BrainPanel } from "./features/brain/BrainPanel";
import { MemoryPanel } from "./features/memory/MemoryPanel";
import type { MemoryProfileDraft } from "./features/memory/MemoryPanel";
import { PreferencesPanel } from "./features/preferences/PreferencesPanel";
import { ModelPicker } from "./features/chat/ModelPicker";
import { useAttachments } from "./features/chat/useAttachments";
import { ProfileModal } from "./features/profile/ProfileModal";
import { EMPTY_PROFILE } from "./features/profile/useProfile";
import type { ProfileDto } from "./shared/types";
import { Starfield } from "./shared/Starfield";
import { GlassSurface } from "./shared/GlassSurface";
import { KineticText } from "./shared/KineticText";
import { WindowControls } from "./features/shell/WindowControls";
import { RubberNavigation } from "./shared/RubberNavigation";
import { getTheme } from "./shared/theme";
import { applyTheme } from "./shared/themeDom";
import "./shared/settings.css";

const AppsPanel = lazy(() => import("./features/integrations/AppsPanel"));
type View = "chat" | "brain" | "memory" | "preferences" | "apps";
const NAV = [
  { id: "chat", label: "Conversa", icon: "chat" },
  { id: "memory", label: "Memória", icon: "memory" },
  { id: "apps", label: "Apps", icon: "apps" },
  { id: "preferences", label: "Configurações", icon: "settings" },
  { id: "brain", label: "Brain", icon: "brain" },
] as const;

export function App(): React.ReactElement {
  const session = useSession();
  const sendCommand = useMemo<SendCommandFn>(() => {
    const api = getBridgeApi();
    return api ? makeSendCommand(api) : async () => { throw new Error("Ponte preload indisponível."); };
  }, [session.sessionVersion]);
  const chat = useChatTask(sendCommand, session.session, session.sessionVersion);
  const [view, setView] = useState<View>("chat");
  const [historyDrawerOpen, setHistoryDrawerOpen] = useState(false);
  const [revision, setRevision] = useState(0);
  const [brain, setBrain] = useState<BrainGetDto | null>(null);
  const [approval, setApproval] = useState<ApprovalDto | null>(null);
  const [preferences, setPreferences] = useState<UiPreferencesDto | null>(null);
  const [memory, setMemory] = useState<MemoryProfileDto | null>(null);
  // Rascunho do perfil vive aqui (não no módulo): sobrevive à troca de aba, é por sessão e
  // morre com a app — wipe/esquecer nunca o ressuscita.
  const [memoryDraft, setMemoryDraft] = useState<{ sessionId: string } & MemoryProfileDraft | null>(null);
  // BACKGROUND V1: rascunho partilhado com o mini pelo main (mesma assinatura de setter; nunca
  // persistido; snapshot tardio não reverte edição mais nova).
  const [draft, setDraft] = usePresenceDraft(chat.conversationId);
  const [shellError, setShellError] = useState("");
  // Resultado final de uma troca de cérebro que demorou mais que a resposta do comando:
  // o backend publica o estado em `brain.updated` e é aqui que ele fica visível.
  const [brainNotice, setBrainNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [policyBusy, setPolicyBusy] = useState(false);
  const [opened, setOpened] = useState(false);
  const [leaving, setLeaving] = useState(false);
  const [modelBusy, setModelBusy] = useState(false);
  // Estado local da janela (sem preferência nova): abaixo de 900px começa em rail, e o usuário pode inverter em qualquer tamanho.
  const [navCollapsed, setNavCollapsed] = useState(() => window.matchMedia?.("(max-width: 900px)").matches ?? false);
  // Rail de contexto: estado local da janela; Parar e rascunho vivem fora daqui.
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileBusy, setProfileBusy] = useState(false);
  const attachments = useAttachments();
  const [introFinished, setIntroFinished] = useState(false);
  const openedRef = useRef(false);
  const [controlsSession, setControlsSession] = useState("");
  const [memorySession, setMemorySession] = useState("");
  const [startupError, setStartupError] = useState("");
  const [fontReady, setFontReady] = useState(!document.fonts);
  const notifiedSession = useRef("");
  const sessionId = session.session?.session_id ?? "";
  // VOICE REFINEMENTS V2: o ditado vive UMA única vez no App — o controlador de hold funciona
  // com qualquer vista aberta, e o painel do compositor é consumidor da mesma instância (um só
  // consumidor do `voice.input.result`). O modo reportado pelo ChatPanel é o modo do envio
  // automático: a mesma `chat.send`, sem parser, fila ou retry próprios.
  const voiceApi = useVoiceInput(sessionId || undefined, session.session?.capabilities.voice_input === true);
  const warmup = useVoiceWarmup(sessionId || undefined);
  const composerModeRef = useRef<ChatMode>("execute");
  const reportVoiceMode = useCallback((mode: ChatMode) => { composerModeRef.current = mode; }, []);
  const hold = useHoldController({
    api: voiceApi,
    sessionId: sessionId || undefined,
    send: async (mode, text) => {
      const outcome = await chat.send(mode, text);
      return { accepted: outcome.accepted };
    },
    getMode: () => composerModeRef.current,
  });
  // Identidade estável: o painel depende disto em useCallback; sem isto, cada render recria a
  // carga do perfil e transforma o `memory.profile.get` em loop de IPC.
  const onMemoryDraftChange = useCallback((next: MemoryProfileDraft | null) => {
    setMemoryDraft(next ? { ...next, sessionId } : null);
  }, [sessionId]);
  const themeId = getTheme(preferences?.theme).id;
  // The approved intro remains celestial; apply the saved work theme when the shell mounts.
  useLayoutEffect(() => { applyTheme(opened ? themeId : "celestial"); }, [themeId, opened]);

  useEffect(() => {
    const motion = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const timer = window.setTimeout(() => setIntroFinished(true), INTRO_DURATION_MS);
    const finishIfReduced = () => {
      if (motion?.matches) { window.clearTimeout(timer); setIntroFinished(true); }
    };
    finishIfReduced();
    motion?.addEventListener("change", finishIfReduced);
    return () => { window.clearTimeout(timer); motion?.removeEventListener("change", finishIfReduced); };
  }, []);

  useEffect(() => {
    if (!document.fonts) return;
    let alive = true;
    // A font failure must not block a working application; the installed sans is the fallback.
    const timer = window.setTimeout(() => { if (alive) setFontReady(true); }, 2000);
    void document.fonts.load('500 12px "Plus Jakarta Sans"').catch(() => undefined).finally(() => {
      window.clearTimeout(timer);
      if (alive) setFontReady(true);
    });
    return () => { alive = false; window.clearTimeout(timer); };
  }, []);

  useEffect(() => {
    const mq = window.matchMedia?.("(max-width: 900px)");
    if (!mq) return;
    const sync = (event: MediaQueryListEvent) => { setNavCollapsed(event.matches); setHistoryDrawerOpen(false); };
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    const api = getBridgeApi();
    if (!api) return;
    const unsubscribe = api.onEvent((event) => {
      // BACKGROUND V1: o main pede uma rota (definições do companheiro ou volta ao chat).
      if (event.type === "shell.route") {
        const view = (event.payload as { view?: unknown }).view;
        if (view === "preferences" || view === "chat") setView(view);
        return;
      }
      if (event.type !== "brain.updated") return;
      const payload = event.payload as unknown as BrainStateDto;
      // O estado publicado é autoritativo: aplica-o sem esperar outra navegação/consulta.
      setBrain(previous => (previous ? { ...previous, current: { ...payload } } : previous));
      if (payload.ok === false) {
        setBrainNotice({
          text: `O cérebro não mudou: ${payload.error?.message ?? "a aplicação falhou"}`
            + (payload.error?.stage ? ` (${payload.error.stage})` : ""),
          error: true,
        });
      } else if (payload.persisted === false) {
        const suffix = payload.error?.message ? ` ${payload.error.message}` : "";
        setBrainNotice({
          text: `Cérebro aplicado nesta sessão, mas a seleção não foi gravada; ela pode não voltar no próximo arranque.${suffix}`,
          error: true,
        });
      } else {
        setBrainNotice(null);
      }
      setRevision(r => r + 1);
    });
    return () => unsubscribe();
  }, [session.sessionVersion]);

  useEffect(() => {
    if (session.status !== "ready") return;
    let alive = true;
    const available = Object.values(session.session?.capabilities ?? {}).some(Boolean);
    const pending = available ? Promise.all([
      session.session?.capabilities.brain ? sendCommand<BrainGetDto>("brain.get", {}) : Promise.resolve(null),
      sendCommand<ApprovalDto>("approval.get", {}),
      sendCommand<UiPreferencesDto>("ui.preferences.get", {}),
    ]) : Promise.resolve([null, null, null] as const);
    pending.then(([b, a, p]) => {
      if (alive) { setBrain(b); setApproval(a); setPreferences(p); setShellError(""); setControlsSession(sessionId); }
    }).catch(() => { if (alive) {
      setShellError("Não foi possível atualizar os controles. Abra Configurações para verificar.");
      if (!openedRef.current) setStartupError("Não foi possível carregar Brain e preferências.");
    } });
    return () => { alive = false; };
  }, [sendCommand, session.status, revision, view, chat.task?.phase, sessionId]);

  useEffect(() => {
    let alive = true;
    setMemory(null);
    setMemoryDraft(current => (current && current.sessionId === sessionId ? current : null));
    if (session.status === "ready") {
      const pending = session.session?.capabilities.memory
        ? sendCommand<MemoryProfileDto>("memory.profile.get", {}) : Promise.resolve<MemoryProfileDto>({ enabled: false, auto_learn: false, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "disabled" });
      pending.then(data => {
        if (alive) { setMemory(data); setMemorySession(sessionId); }
      }).catch(() => { if (alive) {
        setMemory({ enabled: false, auto_learn: false, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "disabled" });
        if (!openedRef.current) setStartupError("Não foi possível carregar a memória local.");
        else setShellError("Não foi possível atualizar a memória local. Abra Memória para verificar.");
      } });
    }
    return () => { alive = false; };
  }, [sendCommand, session.status, chat.memoryVersion, view, revision, sessionId]);

  const initialized = session.status === "ready" && Boolean(sessionId)
    && controlsSession === sessionId && memorySession === sessionId && fontReady && !startupError
    && warmup.settled;
  useEffect(() => {
    if (!initialized || !introFinished || opened) { setLeaving(false); return; }
    const finish = () => { openedRef.current = true; setOpened(true); };
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { finish(); return; }
    setLeaving(true);
    const timer = window.setTimeout(finish, 260);
    return () => window.clearTimeout(timer);
  }, [initialized, introFinished, opened]);
  useEffect(() => {
    if (!opened || !initialized || notifiedSession.current === sessionId) return;
    const frame = requestAnimationFrame(() => {
      notifiedSession.current = sessionId;
      void getBridgeApi()?.notifyRendererReady?.().catch(() => undefined);
    });
    return () => cancelAnimationFrame(frame);
  }, [opened, initialized, sessionId]);

  const profileName = preferences?.profile?.name?.trim() ?? "";
  const profile: ProfileDto = preferences?.profile
    ? { name: preferences.profile.name ?? "", avatar_data_url: preferences.profile.avatar_data_url ?? "" }
    : EMPTY_PROFILE;
  const saveProfile = async (next: ProfileDto): Promise<string | null> => {
    setProfileBusy(true);
    try {
      const saved = await sendCommand<{ profile?: { name?: unknown; avatar_data_url?: unknown } }>(
        "ui.preferences.set", { profile: { name: next.name, avatar_data_url: next.avatar_data_url } });
      // Só mostro o perfil como salvo se o backend devolveu o DTO canônico — sem fingir persistência.
      const canonicalName = saved?.profile?.name;
      const canonicalAvatar = saved?.profile?.avatar_data_url;
      if (typeof canonicalName !== "string" || typeof canonicalAvatar !== "string") {
        return "O backend não confirmou o salvamento do perfil.";
      }
      setPreferences(current => ({
        ...(current ?? { window: null, scale_pct: 100, actions: [] }),
        profile: { name: canonicalName, avatar_data_url: canonicalAvatar },
      }));
      setProfileOpen(false);
      return null;
    } catch {
      return "Não foi possível confirmar o salvamento do perfil.";
    } finally {
      setProfileBusy(false);
    }
  };
  const changePolicy = async (enabled: boolean) => {
    setPolicyBusy(true);
    try { setApproval(await sendCommand<ApprovalDto>("approval.set", { auto_approve: enabled })); }
    catch { setShellError("Não foi possível alterar a autoaprovação. O modo anterior foi mantido."); }
    finally { setPolicyBusy(false); }
  };

  if (!opened) return <>
    <WindowControls />
    <StartupScreen
    leaving={leaving}
    error={session.status === "failed" || Boolean(startupError)}
    message={session.status === "failed" ? "Não foi possível iniciar o backend Python."
      : startupError || (session.status === "ready"
        ? (warmup.settled ? "Carregando preferências e memória…" : "A preparar a voz…")
        : "Iniciando sistema…")}
    onRetry={() => {
      setControlsSession("");
      setMemorySession("");
      setStartupError("");
      if (session.status === "failed") void session.restart();
      else setRevision(r => r + 1);
    }} />
  </>;

  return (
    <div className="lunmi-shell lunmi-shell-enter">
      <Starfield themeId={themeId} />
      <aside className="lunmi-sidebar" aria-label="Navegação principal" data-collapsed={navCollapsed}>
        <div className="lunmi-brand">
          {/* Canto superior esquerdo: o avatar do usuário ocupa o lugar da logo. É o único
              controle ali e abre o modal do perfil. */}
          <button type="button" className="lunmi-profile-mini" data-testid="profile-button"
            aria-label={profileName ? `Perfil de ${profileName}` : "Seu perfil"}
            title={profileName || "Seu perfil"} onClick={() => setProfileOpen(true)}>
            {profile.avatar_data_url
              ? <img className="lunmi-profile-thumb" src={profile.avatar_data_url} alt="" />
              : <span className="lunmi-profile-fallback" aria-hidden="true"><Icon name="user" size={18} /></span>}
          </button>
        </div>
        {/* Controle de recolher/expandir imediatamente acima da casca, fora do vidro — como o painel direito. */}
        <div className="lunmi-nav-bar">
          <button type="button" className="lunmi-nav-collapse" data-testid="nav-collapse-toggle"
            aria-expanded={!navCollapsed} aria-label={navCollapsed ? "Expandir navegação" : "Recolher navegação"}
            title={navCollapsed ? "Expandir navegação" : "Recolher navegação"}
            onClick={() => { setNavCollapsed(value => !value); setHistoryDrawerOpen(false); }}>
            <Icon name={navCollapsed ? "expand" : "collapse"} size={20} />
          </button>
        </div>
        <GlassSurface radius={18} className="lunmi-sidebar-glass">
          <div className="lunmi-sidebar-inner">
        <RubberNavigation items={NAV} value={view} disabled={modelBusy} onChange={next => {
          setView(next as View);
          if (next === 'chat' && navCollapsed) setHistoryDrawerOpen(true);
        }} branch={session.session?.capabilities.chat_history === true ? {
          id: 'chat', controls: 'conversation-history', expanded: navCollapsed ? historyDrawerOpen : view === 'chat',
          content: <HistoryNavigation
          sendCommand={sendCommand} activeId={chat.conversationId} collapsed={navCollapsed}
          expanded={view === 'chat'} drawerOpen={historyDrawerOpen} onDrawerClose={() => setHistoryDrawerOpen(false)}
          locked={chat.busy || chat.submitting || modelBusy || voiceApi.isActive}
          onSelected={() => { setView('chat'); attachments.clear(); voiceApi.dismissTranscript(); }} />,
        } : undefined} />
          </div>
        </GlassSurface>
      </aside>
      <div className="lunmi-shell-main">
        <header className="lunmi-topbar" data-rail={view === "chat" ? (railCollapsed ? "edge" : "open") : "none"}>
          {/* Marca central: somente “Lunmi”, sem cápsula, com Kinetic Text (Magic UI): as letras
              reagem ao ponteiro mudando de peso. Fica no meio, grande mas contida. */}
          <KineticText text="Lunmi" className="lunmi-brand-mark" data-testid="brand-text" title="Lunmi" />
          <WindowControls />
        </header>
        {session.status === "failed" && (
          <SessionFailureStrip failure={session.failure} errorMessage={session.errorMessage}
            onRestart={() => void session.restart()} />
        )}
        {shellError && <p role="alert" className="lunmi-shell-error">{shellError}</p>}
        {warmup.settledMessage && (
          <p role="status" className="lunmi-shell-error" data-testid="voice-notice">{warmup.settledMessage}</p>
        )}
        {brainNotice && (
          <p role={brainNotice.error ? "alert" : "status"} className="lunmi-shell-error"
            data-testid="brain-notice">{brainNotice.text}</p>
        )}
        {view !== "chat" && chat.busy && <div className="lunmi-taskstrip">
          <span className="lunmi-taskstrip-text">{chat.confirmRequest || chat.inputRequest ? "A tarefa precisa da sua resposta." : chat.task?.progress || "Tarefa em andamento"}</span>
          <button type="button" onClick={() => setView("chat")}>Voltar à tarefa</button>
          <button type="button" data-testid="stop-task" onClick={() => void chat.stop()}>Parar</button>
        </div>}
        <div className="lunmi-shell-body">
          <main className="lunmi-main">
            <div className="lunmi-chat-view" hidden={view !== "chat"}>
              {chat.history && (chat.history.conversation.temporary || !chat.history.options.enabled) && <p className="lunmi-history-banner" role="status">
                {chat.history.conversation.temporary ? 'Conversa temporária · as mensagens não são guardadas no histórico.'
                  : !chat.history.options.enabled ? 'Gravação desligada · as próximas mensagens ficam só nesta sessão.'
                  : chat.history.conversation.title}
              </p>}
              {chat.history?.error && <p className="lunmi-history-banner" role="alert">{chat.history.error}</p>}
              {chat.history?.has_more && <button type="button" className="lunmi-link" disabled={chat.busy}
                onClick={() => void sendCommand('chat.history.read', { conversation_id: chat.conversationId,
                  before: chat.history?.before }).catch(err => setShellError(String(err.message ?? 'Não foi possível carregar mensagens.')))}>Carregar mensagens anteriores</button>}
              <ChatPanel chat={chat} session={session.session} draft={draft} setDraft={setDraft} modelChanging={modelBusy}
                voiceInput={{ api: voiceApi, holdNotice: hold.notice }} reportVoiceMode={reportVoiceMode}
                autoApprove={{ ready: Boolean(approval), enabled: approval?.auto_approve ?? false, busy: policyBusy || modelBusy, onToggle: next => void changePolicy(next) }}
                attachments={attachments.items}
                attachmentPayload={() => attachments.items.map(item => ({ name: item.name, text: attachments.contents.get(item.id) ?? "", size_bytes: item.size }))}
                onPickAttachment={() => void attachments.pick()} onRemoveAttachment={attachments.remove}
                attachSupported={attachments.supported} attachPicking={attachments.picking}
                attachNotice={attachments.notice} onDismissAttachNotice={attachments.dismissNotice}
                onAttachmentsAccepted={attachments.clear}
                modelPicker={<ModelPicker key={brain?.current.provider ?? ""} brain={brain} sendCommand={sendCommand} changing={modelBusy}
                  disabled={chat.busy || chat.submitting || session.status !== "ready" || controlsSession !== sessionId || session.session?.capabilities.brain !== true}
                  onBusyChange={setModelBusy} onChanged={current => { setBrain(previous => previous ? { ...previous, current } : previous); setRevision(r => r + 1); }}
                  onProviderSettings={() => setView("brain")} />} />
            </div>
            {view === "brain" && <BrainPanel sendCommand={sendCommand} session={session.session} onConfigured={() => setRevision(r => r + 1)} />}
            {view === "apps" && <Suspense fallback={<p role="status">A carregar Apps…</p>}>
              <AppsPanel sendCommand={sendCommand} session={session.status === "ready" ? session.session : null} />
            </Suspense>}
            {view === "memory" && <MemoryPanel sendCommand={sendCommand} session={session.session} refreshToken={chat.memoryVersion}
              draft={memoryDraft && memoryDraft.sessionId === sessionId ? { text: memoryDraft.text, baseRevision: memoryDraft.baseRevision } : null}
              onDraftChange={onMemoryDraftChange} />}
            {view === "preferences" && <PreferencesPanel sendCommand={sendCommand} session={session.session}
              history={chat.history} historyLocked={chat.busy || chat.submitting}
              onThemeChanged={theme => { setPreferences(current => current ? { ...current, theme } : current); setRevision(r => r + 1); }} />}
          </main>
          {view === "chat" && (railCollapsed ? (
            <aside className="lunmi-rail-edge" aria-label="Painel de contexto">
              <button type="button" className="lunmi-rail-expand" data-testid="rail-collapse-toggle"
                aria-expanded={false} aria-label="Mostrar painel de contexto" title="Mostrar painel de contexto"
                onClick={() => setRailCollapsed(false)}>
                <Icon name="chevron" size={15} />
              </button>
            </aside>
          ) : (
          <aside className="lunmi-rail" aria-label="Contexto da conversa">
            <div className="lunmi-rail-bar">
              <span className="lunmi-rail-title">Contexto</span>
              <button type="button" className="lunmi-rail-collapse" data-testid="rail-collapse-toggle"
                aria-expanded aria-label="Recolher painel de contexto" title="Recolher painel de contexto"
                onClick={() => setRailCollapsed(true)}>
                <Icon name="collapse" size={16} />
              </button>
            </div>
            <GlassSurface radius={18} className="lunmi-rail-glass">
            <div className="lunmi-rail-inner">
            <section className="lunmi-rail-section"><h2>Ações rápidas</h2><p className="lunmi-rail-empty">Preenchem a mensagem. Você decide quando enviar.</p>
              {(preferences?.actions ?? []).map((action, index) => <button className="lunmi-row" type="button" key={index}
                disabled={Boolean(draft.trim())} title={draft.trim() ? "Termine ou limpe o rascunho antes de usar um atalho" : "Preencher mensagem"}
                onClick={() => { setDraft(current => current.trim() ? current : action.draft); document.querySelector<HTMLTextAreaElement>('[data-testid="composer"]')?.focus(); }}>
                <Icon name="plus" /><span className="lunmi-row-text">{action.label}</span><Icon name="chevron" size={14} />
              </button>)}
              {!preferences?.actions?.length && <p className="lunmi-rail-empty">Crie seus atalhos em Configurações.</p>}
            </section>
            <section className="lunmi-rail-section"><h2>Tarefas recentes</h2>
              {chat.recentTasks.length ? chat.recentTasks.map(task => <div className="lunmi-recent" key={task.task_id}>
                <span className={`lunmi-status-dot ${task.phase}`} aria-hidden="true" /><div><p>{task.instruction}</p><small>{task.result?.status ?? task.phase}</small></div>
              </div>) : <p className="lunmi-rail-empty">Nenhuma tarefa nesta sessão.</p>}
              <p className="lunmi-rail-note">O histórico anterior não é recuperado aqui.</p>
            </section>
            <section className="lunmi-rail-section"><h2>Resumo de memória</h2>
              {memory === null ? <p className="lunmi-rail-empty">Carregando memória…</p> : !memory.enabled ? <p className="lunmi-rail-empty">Memória desligada.</p>
                : memory.text.trim() ? memory.text.split(/\n\s*\n/).filter(p => p.trim()).slice(0, 3).map((paragraph, index) => <p className="lunmi-memory-item" key={index}><Icon name="memory" size={16} />{paragraph.trim()}</p>)
                : <p className="lunmi-rail-empty">Você controla o que a Lunmi lembra.</p>}
              <button className="lunmi-link" type="button" onClick={() => setView("memory")}>Gerenciar memória</button>
            </section>
            </div>
            </GlassSurface>
          </aside>
          ))}
        </div>
      </div>
      {profileOpen && (
        <ProfileModal profile={profile} busy={profileBusy}
          onClose={() => setProfileOpen(false)}
          onSave={async next => await saveProfile(next)} />
      )}
    </div>
  );
}
