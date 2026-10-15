import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { getBridgeApi, makeSendCommand, type SendCommandFn } from "./shared/ipc/client";
import { useSession } from "./shared/ipc/useSession";
import { BrandMark, Icon } from "./shared/ui";
import type { ApprovalDto, BrainGetDto, MemoryListDto, UiPreferencesDto } from "./shared/types";
import { SessionStatusChip, SessionFailureStrip } from "./features/session/SessionState";
import { INTRO_DURATION_MS, StartupScreen } from "./features/session/StartupScreen";
import { ChatPanel } from "./features/chat/ChatPanel";
import { useChatTask } from "./features/chat/useChatTask";
import { BrainPanel } from "./features/brain/BrainPanel";
import { MemoryPanel } from "./features/memory/MemoryPanel";
import { PreferencesPanel } from "./features/preferences/PreferencesPanel";
import { ModelPicker } from "./features/chat/ModelPicker";
import { useAttachments } from "./features/chat/useAttachments";
import { ProfileModal } from "./features/profile/ProfileModal";
import { EMPTY_PROFILE } from "./features/profile/useProfile";
import type { ProfileDto } from "./shared/types";
import { Starfield } from "./shared/Starfield";
import { RubberNavigation } from "./shared/RubberNavigation";
import { getTheme } from "./shared/theme";
import { applyTheme } from "./shared/themeDom";

type View = "chat" | "brain" | "memory" | "preferences";
const NAV = [
  { id: "chat", label: "Conversa", icon: "chat" },
  { id: "memory", label: "Memória", icon: "memory" },
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
  const [revision, setRevision] = useState(0);
  const [brain, setBrain] = useState<BrainGetDto | null>(null);
  const [approval, setApproval] = useState<ApprovalDto | null>(null);
  const [preferences, setPreferences] = useState<UiPreferencesDto | null>(null);
  const [memory, setMemory] = useState<MemoryListDto | null>(null);
  const [draft, setDraft] = useState("");
  const [shellError, setShellError] = useState("");
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
    const sync = (event: MediaQueryListEvent) => setNavCollapsed(event.matches);
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

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
    if (session.status === "ready") {
      const pending = session.session?.capabilities.memory
        ? sendCommand<MemoryListDto>("memory.list", {}) : Promise.resolve({ enabled: false, items: [] });
      pending.then(data => {
        if (alive) { setMemory(data); setMemorySession(sessionId); }
      }).catch(() => { if (alive) {
        setMemory({ enabled: false, items: [] });
        if (!openedRef.current) setStartupError("Não foi possível carregar a memória local.");
        else setShellError("Não foi possível atualizar a memória local. Abra Memória para verificar.");
      } });
    }
    return () => { alive = false; };
  }, [sendCommand, session.status, chat.memoryVersion, view, revision, sessionId]);

  const initialized = session.status === "ready" && Boolean(sessionId)
    && controlsSession === sessionId && memorySession === sessionId && fontReady && !startupError;
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
  // O modelo exibido é sempre o real. Vazio significa padrão/não identificado — nunca um nome inventado.
  const modelLabel = brain?.current.model?.trim() || "Modelo padrão";
  const modelTitle = brain?.current.model?.trim()
    ? `Modelo em uso: ${brain.current.model}`
    : "Nenhum modelo identificado; o provedor usará o padrão dele.";
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

  if (!opened) return <StartupScreen
    leaving={leaving}
    error={session.status === "failed" || Boolean(startupError)}
    message={session.status === "failed" ? "Não foi possível iniciar o backend Python."
      : startupError || (session.status === "ready" ? "Carregando preferências e memória…" : "Iniciando sistema…")}
    onRetry={() => {
      setControlsSession("");
      setMemorySession("");
      setStartupError("");
      if (session.status === "failed") void session.restart();
      else setRevision(r => r + 1);
    }} />;

  return (
    <div className="lunmi-shell lunmi-shell-enter">
      <Starfield themeId={themeId} />
      <aside className="lunmi-sidebar" aria-label="Navegação principal" data-collapsed={navCollapsed}>
        <div className="lunmi-brand">
          <BrandMark size={30} /><span className="lunmi-brand-word">LUNMI</span>
          <button type="button" className="lunmi-nav-collapse" data-testid="nav-collapse-toggle"
            aria-expanded={!navCollapsed} aria-label={navCollapsed ? "Expandir navegação" : "Recolher navegação"}
            title={navCollapsed ? "Expandir navegação" : "Recolher navegação"}
            onClick={() => setNavCollapsed(value => !value)}>
            <Icon name={navCollapsed ? "expand" : "collapse"} size={20} />
          </button>
        </div>
        <RubberNavigation items={NAV} value={view} disabled={modelBusy} onChange={next => setView(next as View)} />
        <div className="lunmi-sidebar-foot">
          <button type="button" className="lunmi-profile-mini" data-testid="profile-button"
            aria-label={profileName ? `Perfil de ${profileName}` : "Seu perfil"}
            title={profileName || "Seu perfil"} onClick={() => setProfileOpen(true)}>
            {profile.avatar_data_url
              ? <img className="lunmi-profile-thumb" src={profile.avatar_data_url} alt="" />
              : <span className="lunmi-profile-fallback" aria-hidden="true"><Icon name="user" size={16} /></span>}
            <span className="lunmi-nav-text lunmi-profile-name">{profileName || "Seu perfil"}</span>
          </button>
        </div>
      </aside>
      <div className="lunmi-shell-main">
        <header className="lunmi-topbar">
          <label className="lunmi-toggle lunmi-topbar-left" title="Vale para a próxima tarefa. Ações sensíveis continuam exigindo confirmação.">
            <input type="checkbox" aria-label="Autoaprovação" data-testid="header-auto-approve" checked={approval?.auto_approve ?? false}
              disabled={!approval || policyBusy || modelBusy} onChange={e => void changePolicy(e.target.checked)} />
            <span className="lunmi-toggle-track" aria-hidden="true" /><span>Autoaprovação</span>
          </label>
          <span className="lunmi-model-capsule" data-testid="brain-pill" title={modelTitle}>
            <i className="lunmi-capsule-dot" aria-hidden="true" />
            <span className="lunmi-capsule-brand">LUNMI</span>
            <span className="lunmi-capsule-sep" aria-hidden="true">•</span>
            <span className="lunmi-capsule-model" data-testid="topbar-model">{modelLabel}</span>
          </span>
          <span className="lunmi-topbar-right"><SessionStatusChip status={session.status} /></span>
        </header>
        {session.status === "failed" && (
          <SessionFailureStrip failure={session.failure} errorMessage={session.errorMessage}
            onRestart={() => void session.restart()} />
        )}
        {shellError && <p role="alert" className="lunmi-shell-error">{shellError}</p>}
        {view !== "chat" && chat.busy && <div className="lunmi-taskstrip">
          <span className="lunmi-taskstrip-text">{chat.confirmRequest || chat.inputRequest ? "A tarefa precisa da sua resposta." : chat.task?.progress || "Tarefa em andamento"}</span>
          <button type="button" onClick={() => setView("chat")}>Voltar à tarefa</button>
          <button type="button" data-testid="stop-task" onClick={() => void chat.stop()}>Parar</button>
        </div>}
        <div className="lunmi-shell-body">
          <main className="lunmi-main">
            <div className="lunmi-chat-view" hidden={view !== "chat"}>
              <ChatPanel chat={chat} session={session.session} draft={draft} setDraft={setDraft} modelChanging={modelBusy}
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
            {view === "memory" && <MemoryPanel sendCommand={sendCommand} session={session.session} refreshToken={chat.memoryVersion} />}
            {view === "preferences" && <PreferencesPanel sendCommand={sendCommand} session={session.session}
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
                : memory.items.length ? memory.items.slice(0, 4).map(item => <p className="lunmi-memory-item" key={item.id}><Icon name="memory" size={16} />{item.text}</p>)
                : <p className="lunmi-rail-empty">Você controla o que a Lunmi lembra.</p>}
              <button className="lunmi-link" type="button" onClick={() => setView("memory")}>Gerenciar memória</button>
            </section>
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
