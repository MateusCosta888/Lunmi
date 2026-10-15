/**
 * Controles próprios da janela (`desktop/src/features/shell/WindowControls.tsx`).
 *
 * Substituem o `titleBarOverlay` nativo: três botões fixos no canto superior direito, centrados
 * verticalmente em relação ao cabeçalho, sem blur/filtro/halo no hover (só cor de superfície).
 * Só falam com o IPC exclusivo pelo enum `minimize | toggle-maximize | close | state`; o estado
 * maximizado vem dos eventos nativos (`window.state`), não do clique. Sem a ponte (testes/
 * desenvolvimento), ficam desabilitados com motivo honesto.
 */
import React, { useEffect, useState } from "react";
import { getBridgeApi } from "../../shared/ipc/client";
import type { WindowStateDto } from "../../shared/ipc/client";
import { Icon } from "../../shared/ui";

export function WindowControls(): React.ReactElement {
  const api = getBridgeApi();
  const supported = typeof api?.windowControl === "function";
  const [state, setState] = useState<WindowStateDto>({ minimized: false, maximized: false });
  const [headerHeight, setHeaderHeight] = useState<number | null>(null);

  // O topo cresce quando os chips quebram linha (ex.: 700×560 → ~83px). Os controles acompanham a
  // altura REAL medida do cabeçalho; sem cabeçalho (splash) mantêm a altura padrão do CSS.
  useEffect(() => {
    const bar = document.querySelector<HTMLElement>(".lunmi-topbar");
    if (!bar) return;
    const read = () => setHeaderHeight(bar.getBoundingClientRect().height);
    read();
    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") { observer = new ResizeObserver(read); observer.observe(bar); }
    window.addEventListener("resize", read);
    return () => { observer?.disconnect(); window.removeEventListener("resize", read); };
  }, []);

  useEffect(() => {
    if (!supported) return;
    let alive = true;
    void api!.windowControl!("state").then(next => { if (alive && next) setState(next); }).catch(() => undefined);
    const unsubscribe = api!.onEvent((event) => {
      if (event.type === "window.state") {
        const payload = event.payload as Partial<WindowStateDto>;
        setState({ minimized: Boolean(payload.minimized), maximized: Boolean(payload.maximized) });
      }
    });
    return () => { alive = false; unsubscribe(); };
  }, [api, supported]);

  const run = (action: "minimize" | "toggle-maximize" | "close") => {
    if (!supported) return;
    void api!.windowControl!(action).then(next => { if (next) setState(previous => ({ ...previous, minimized: next.minimized, maximized: next.maximized })); }).catch(() => undefined);
  };
  const title = supported ? undefined : "Controles de janela indisponíveis sem a ponte";

  return (
    <div className="lunmi-wincontrols" data-testid="window-controls" role="group" aria-label="Controles da janela"
      style={headerHeight ? { height: `${Math.round(headerHeight)}px` } : undefined }>
      <button type="button" className="lunmi-wincontrol" data-testid="win-minimize" aria-label="Minimizar" title="Minimizar"
        disabled={!supported} onClick={() => run("minimize")}>
        <Icon name="win-min" size={15} />
      </button>
      <button type="button" className="lunmi-wincontrol" data-testid="win-maximize"
        aria-label={state.maximized ? "Restaurar" : "Maximizar"} title={state.maximized ? "Restaurar" : "Maximizar"}
        disabled={!supported} onClick={() => run("toggle-maximize")}>
        <Icon name={state.maximized ? "win-restore" : "win-max"} size={13} />
      </button>
      <button type="button" className="lunmi-wincontrol lunmi-wincontrol-close" data-testid="win-close" aria-label="Fechar" title="Fechar"
        disabled={!supported} onClick={() => run("close")}>
        <Icon name="win-close" size={15} />
      </button>
      {!supported && <span className="lunmi-sr-only">{title}</span>}
    </div>
  );
}
