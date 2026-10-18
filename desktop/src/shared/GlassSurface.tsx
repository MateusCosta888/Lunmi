/**
 * Camada decorativa de liquid glass (`desktop/src/shared/GlassSurface.tsx`).
 *
 * Usa `liquid-glass-react` (MIT) de verdade — refração por SVG displacement + backdrop-filter sobre
 * o cosmos — como pano de fundo isolado do fluxo: o pacote renderiza um Fragment com quatro irmãos
 * utilitários (`top:50%/left:50%` + translate) além do container; todos ficam dentro de
 * `.lunmi-glass-layer` (absolute, pointer-events none, aria-hidden) e o transform residual é
 * neutralizado por CSS. O conteúdo é IRMÃO da camada, com altura e rolagem próprias — nada do
 * layout do app depende da árvore interna do pacote. Sem elasticidade e sem listener de mouse do
 * pacote (`globalMousePos`/`mouseOffset` estáticos pulam o efeito de mousemove), modo standard,
 * refração discreta, estático em movimento reduzido, tint dos tokens.
 *
 * O mecanismo "bento light" (adaptado do MagicBento/React Bits; ver `bento.tsx`) observa o
 * ponteiro nesta mesma superfície: spotlight e contorno seguem com fade curto e a onda de clique
 * é finita e decorativa. As camadas são spans sem interação — nada captura clique, arrasto ou
 * seleção. A media query CSS `prefers-reduced-transparency` desliga o vidro e os brilhos,
 * mantendo o fallback sólido mesmo quando outra superfície é desmontada.
 */
import React, { useEffect, useRef, useState } from "react";
import LiquidGlass from "liquid-glass-react";
import { useVisualActivity } from "./useVisualActivity";
import { useBentoSurface } from "./bento";
import { getTheme } from "./theme";

export interface GlassSurfaceProps {
  children: React.ReactNode;
  radius?: number;
  className?: string;
  style?: React.CSSProperties;
}

const STATIC_POS = { x: 0, y: 0 };

/** The active theme id, tracked live so the refraction strength follows a runtime theme switch. */
function useThemeMode(): "light" | "dark" {
  const read = () => getTheme(document.documentElement.dataset.theme).mode;
  const [mode, setMode] = useState<"light" | "dark">(read);
  useEffect(() => {
    if (typeof MutationObserver === "undefined") return;
    const observer = new MutationObserver(() => setMode(read()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    setMode(read());
    return () => observer.disconnect();
  }, []);
  return mode;
}

export function GlassSurface({ children, radius = 18, className = "", style }: GlassSurfaceProps): React.ReactElement {
  const { reduced } = useVisualActivity();
  const themeMode = useThemeMode();
  const rootRef = useRef<HTMLDivElement>(null);
  useBentoSurface(rootRef, { wave: true, intro: true });
  // Light themes need a stronger displacement of the real backdrop to read as glass; Celestial is
  // approved and keeps its current refraction.
  const displacement = reduced ? 0 : themeMode === "light" ? 34 : 22;
  return (
    <div ref={rootRef} className={`lunmi-glass ${className}`} style={{ borderRadius: radius, ...style }}>
      <div className="lunmi-glass-layer" aria-hidden="true">
        <LiquidGlass
          mode="standard"
          cornerRadius={radius}
          displacementScale={displacement}
          aberrationIntensity={1}
          blurAmount={0.015}
          saturation={125}
          elasticity={0}
          padding="0"
          globalMousePos={STATIC_POS}
          mouseOffset={STATIC_POS}
          style={{ position: "absolute", top: "0px", left: "0px", width: "100%", height: "100%" }}
        >
          <></>
        </LiquidGlass>
      </div>
      <span className="lunmi-bento-light" aria-hidden="true" />
      <span className="lunmi-bento-ring" aria-hidden="true" />
      <div className="lunmi-glass-content">{children}</div>
    </div>
  );
}
