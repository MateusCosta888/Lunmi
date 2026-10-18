// The existing Starfield entry point now hosts the universal React Bits Particles background.
import React from "react";
import Particles from "./Particles";
import { getTheme } from "./theme";
import { useVisualActivity } from "./useVisualActivity";

export function Starfield({ paused = false, themeId = "celestial" }: { paused?: boolean; themeId?: string }): React.ReactElement {
  const { reduced, visible } = useVisualActivity();
  const theme = getTheme(themeId);
  const colors = theme.mode === "dark" ? ["#ffffff", "#ffffff", "#ffffff"]
    : [theme.colors.accent, theme.colors.accentHover, theme.colors.accentPressed];
  const stopped = paused || reduced || !visible;
  return <div className="lunmi-cosmos" aria-hidden="true" data-testid="starfield" data-paused={stopped} data-engine="react-bits-particles">
    <Particles particleColors={colors} particleCount={460} particleSpread={20} speed={0.1}
      particleBaseSize={95} glow={1.5} twinkle={0.7} moveParticlesOnHover hoverEffect="parallax"
      sizeRandomness={1} shape="circle" depthOfField={0} connections connectionDistance={110}
      particleHoverFactor={1} hoverRadius={140} drift="none" driftSpeed={1} alphaParticles={false}
      disableRotation={false} cameraDistance={20} variation={5} opacity={1} clickBurst intro
      lightMode={theme.mode === "light"} paused={stopped} />
  </div>;
}
