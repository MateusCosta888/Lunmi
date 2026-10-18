import { useEffect, useId, useRef, useState } from "react";
import type { BrainGetDto, BrainStateDto, ModelListingDto } from "../../shared/types";
import { getBridgeApi, toBridgeError, type SendCommandFn } from "../../shared/ipc/client";
import { Icon } from "../../shared/ui";
import { useVisualActivity } from "../../shared/useVisualActivity";

export function ModelPicker({ brain, sendCommand, disabled, changing, onBusyChange, onChanged, onProviderSettings }: {
  brain: BrainGetDto | null; sendCommand: SendCommandFn; disabled: boolean; changing: boolean;
  onBusyChange: (busy: boolean) => void; onChanged: (state: BrainStateDto) => void; onProviderSettings: () => void;
}) {
  const [open, setOpen] = useState(false);
  // O menu fecha com saída visível; durante a saída a seleção lógica já foi bloqueada.
  const [closing, setClosing] = useState(false);
  const [listing, setListing] = useState<ModelListingDto | null>(null);
  const [cycle, setCycle] = useState(0);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null), search = useRef<HTMLInputElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const exitAnimation = useRef<Animation | null>(null);
  const id = useId();
  const { reduced, visible } = useVisualActivity();
  const provider = brain?.current.provider ?? "";
  const fixed = Boolean(brain?.current.env_overrides?.model);
  const current = brain?.current.model ?? "";

  const hardClose = () => {
    exitAnimation.current?.cancel();
    exitAnimation.current = null;
    setClosing(false);
    setOpen(false);
  };
  const requestClose = (refocus = true) => {
    if (!open) return;
    const animateExit = !reduced && visible && menu.current?.animate;
    if (!animateExit) { hardClose(); } else {
      setClosing(true);
      const animation = menu.current!.animate(
        [{ opacity: 1, transform: "translateY(0) scale(1)" }, { opacity: 0, transform: "translateY(6px) scale(.98)" }],
        { duration: 140, easing: "cubic-bezier(.4, 0, 1, 1)", fill: "forwards" }
      );
      exitAnimation.current = animation;
      animation.finished.then(() => {
        if (exitAnimation.current !== animation) return;
        exitAnimation.current = null;
        setClosing(false);
        setOpen(false);
      }).catch(() => { /* animação cancelada: o reopen cuida do estado */ });
    }
    if (refocus) trigger.current?.focus();
  };
  const reopen = () => {
    exitAnimation.current?.cancel();
    exitAnimation.current = null;
    setClosing(false);
    setOpen(true);
    setCycle((value) => value + 1);
  };

  // O resultado final de uma troca lenta chega por `brain.updated`: o aviso de "em andamento"
  // precisa sair de cena sem esperar uma nova consulta, e a falha real precisa aparecer.
  useEffect(() => {
    const api = getBridgeApi();
    if (!api) return;
    return api.onEvent((event) => {
      if (event.type !== "brain.updated") return;
      const published = event.payload as unknown as BrainStateDto;
      if (published.ok === false) {
        setError(published.error?.message ?? "O cérebro não mudou.");
        return;
      }
      if (published.persisted === false) {
        setError("Modelo aplicado, mas a seleção não foi gravada; pode não voltar no próximo arranque.");
        return;
      }
      setError("");
    });
  }, []);

  useEffect(() => { if (disabled) { hardClose(); setListing(null); } }, [disabled]);
  useEffect(() => {
    if (!open || !provider || disabled) return;
    let alive = true;
    setLoading(true); setListing(null); setError(""); setQuery("");
    search.current?.focus();
    sendCommand<ModelListingDto>("brain.list", { provider, role: "main", force: false }).then(result => {
      if (alive) {
        if (result.provider !== provider) { setError("O provedor mudou. Abra a lista novamente."); return; }
        setListing(result);
      }
    }).catch(() => { if (alive) setError("Não foi possível consultar os modelos. Tente abrir a lista novamente."); })
      .finally(() => { if (alive) setLoading(false); });
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) requestClose(false); };
    document.addEventListener("pointerdown", outside);
    return () => { alive = false; document.removeEventListener("pointerdown", outside); };
  }, [open, cycle, provider, disabled, sendCommand, reduced, visible]);
  useEffect(() => () => { exitAnimation.current?.cancel(); }, []);

  const choose = async (model: string) => {
    if (disabled || changing || fixed || closing) return;
    if (model === current) { requestClose(); return; }
    onBusyChange(true); setError("");
    try {
      // Model-only payload preserves credentials, endpoint and the separate AppAgent selection.
      const state = await sendCommand<BrainStateDto>("brain.set", { model });
      onChanged(state); requestClose();
    } catch (error) {
      // Sem promessa falsa: ou a troca ainda está em andamento (o commit continua no backend e o
      // estado final sai em `brain.updated`), ou falhou de verdade.
      setError(toBridgeError(error).code === "reconfiguring"
        ? "A troca de modelo continua em andamento; o resultado aparece sozinho quando terminar."
        : "Não foi possível confirmar a troca do modelo agora. O estado real aparece ao lado do seletor.");
    }
    finally { onBusyChange(false); }
  };
  const models = listing?.ok ? listing.models.filter(model => `${model.label} ${model.id}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())) : [];
  return <div className="lunmi-model-picker" ref={root} onBlur={event => {
    if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) requestClose(false);
  }} onKeyDown={event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); requestClose(); }
    if (!open || closing) return;
    if (event.key === "Enter" && event.target === search.current) event.preventDefault();
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const options = Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:not(:disabled)') ?? []);
      const index = options.indexOf(document.activeElement as HTMLButtonElement);
      const next = index < 0 ? (event.key === "ArrowDown" ? 0 : options.length - 1)
        : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
      options[next]?.focus();
    }
  }}>
    <button ref={trigger} type="button" className="lunmi-model-trigger" data-testid="composer-model"
      aria-haspopup="dialog" aria-expanded={open && !closing} aria-controls={id}
      disabled={!provider || disabled || changing} title={disabled ? "Disponível com a conexão pronta e sem tarefa em andamento" : current || "Padrão do provedor"}
      onClick={() => { if (open && !closing) requestClose(); else reopen(); }}>
      <i className="lunmi-model-dot" aria-hidden="true" /><span>{changing ? "Trocando modelo…" : current || "Padrão do provedor"}</span><Icon name="chevron" size={13} />
    </button>
    {open && <div ref={menu} className="lunmi-model-menu" id={id} role="dialog" data-state={closing ? "closing" : "open"}
      aria-label="Selecionar modelo" data-testid="model-menu">
      <div className="lunmi-model-heading"><span>{brain?.current.provider_label || provider}</span><small>Modelo da conversa e do planejamento</small></div>
      <input ref={search} aria-label="Buscar modelos" placeholder="Buscar modelo…" value={query} onChange={event => setQuery(event.target.value)} />
      {fixed && <p className="lunmi-model-note">O modelo está definido pela variável de ambiente LLM_MODEL.</p>}
      {loading && <p role="status" className="lunmi-model-note">Consultando modelos…</p>}
      {error && <p role="alert" className="lunmi-model-error">{error}</p>}
      <div role="listbox" aria-label="Modelos do provedor" className="lunmi-model-options">
        {models.map(model => <button key={model.id} type="button" role="option" aria-selected={current === model.id}
          disabled={disabled || changing || fixed || closing} onClick={() => void choose(model.id)} title={model.id}>
          <span>{model.label || model.id}</span><small>{current === model.id ? "Em uso" : model.default ? "Padrão" : ""}</small>
        </button>)}
      </div>
      {!loading && !error && !models.length && <p className="lunmi-model-note">{listing && !listing.ok ? "O provedor não confirmou a lista de modelos. Verifique a conexão nas configurações." : query ? "Nenhum modelo corresponde à busca." : "Nenhum modelo disponível nesta conexão. Verifique o provedor nas configurações."}</p>}
      <button type="button" className="lunmi-model-settings" disabled={changing || closing} onClick={() => { requestClose(); onProviderSettings(); }}><Icon name="settings" size={16} />Configurar ou trocar provedor<Icon name="chevron" size={13} /></button>
    </div>}
  </div>;
}
