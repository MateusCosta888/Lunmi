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
 */
import React from "react";
import LiquidGlass from "liquid-glass-react";
import { useVisualActivity } from "./useVisualActivity";

export interface GlassSurfaceProps {
  children: React.ReactNode;
  radius?: number;
  className?: string;
  style?: React.CSSProperties;
}

const STATIC_POS = { x: 0, y: 0 };

export function GlassSurface({ children, radius = 18, className = "", style }: GlassSurfaceProps): React.ReactElement {
  const { reduced } = useVisualActivity();
  return (
    <div className={`lunmi-glass ${className}`} style={{ borderRadius: radius, ...style }}>
      <div className="lunmi-glass-layer" aria-hidden="true">
        <LiquidGlass
          mode="standard"
          cornerRadius={radius}
          displacementScale={reduced ? 0 : 14}
          aberrationIntensity={1}
          blurAmount={0.05}
          saturation={115}
          elasticity={0}
          padding="0"
          globalMousePos={STATIC_POS}
          mouseOffset={STATIC_POS}
          style={{ position: "absolute", top: "0px", left: "0px", width: "100%", height: "100%" }}
        >
          <></>
        </LiquidGlass>
      </div>
      <div className="lunmi-glass-content">{children}</div>
    </div>
  );
}
