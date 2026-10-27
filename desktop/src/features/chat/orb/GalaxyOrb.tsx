/**
 * Orb reativa da Lunmi (`desktop/src/features/chat/orb/GalaxyOrb.tsx`, ORB REACTIVE V1).
 *
 * O nome do componente mantém-se para não quebrar consumidores; o motor passou a ser o
 * `liveRenderer` (esfera particulada + olhos do shader original, fiel ao ficheiro fornecido
 * pelo utilizador). O React é dono do ciclo de vida: `setMode` pelos estados reais, `setActive`
 * quando a camada está oculta, `destroy` no desmontar (RAF, listeners, observers, recursos
 * WebGL). Sem contexto 2D/WebGL (teste/ambiente degradado) mostra a marca real — fallback
 * honesto, nunca uma orb falsa por CSS.
 */
import React, { useEffect, useRef, useState } from "react";
import { BrandMark } from "../../../shared/ui";
import { mountLiveOrb } from "./liveRenderer";
import type { LiveOrbHandle } from "./liveRenderer";
import type { OrbMode } from "./orbState";
import "./orb.css";

export type { OrbMode } from "./orbState";

export interface GalaxyOrbProps {
  mode?: OrbMode;
  /** `false` quando a camada/superfície está oculta: o loop para sem desmontar. */
  active?: boolean;
  /** Miniatura do Node (52×52): menos partículas, textura e DPR adaptados. */
  compact?: boolean;
}

export function GalaxyOrb({ mode = "idle", active = true, compact = false }: GalaxyOrbProps): React.ReactElement {
  const canvas = useRef<HTMLCanvasElement>(null);
  const handleRef = useRef<LiveOrbHandle | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    if (!canvas.current) return;
    let destroyed = false;
    const handle = mountLiveOrb(canvas.current, {
      compact,
      onUnavailable: () => { if (!destroyed) setUnavailable(true); },
    });
    handleRef.current = handle;
    return () => {
      destroyed = true;
      handleRef.current = null;
      handle.destroy();
    };
  }, [compact]);

  useEffect(() => { handleRef.current?.setMode(mode); }, [mode]);
  useEffect(() => { handleRef.current?.setActive(active && !unavailable); }, [active, unavailable]);

  return <div className={`lunmi-galaxy-orb${compact ? " lunmi-galaxy-orb-compact" : ""}`}
    data-testid="chat-orb" data-mode={mode} data-compact={compact ? "true" : "false"}
    data-unavailable={unavailable}
    role="img" aria-label="Lunmi">
    <canvas ref={canvas} aria-hidden="true" />
    {unavailable && <BrandMark size={compact ? 26 : 84} />}
  </div>;
}
