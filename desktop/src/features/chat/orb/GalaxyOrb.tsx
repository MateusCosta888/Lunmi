import React, { useEffect, useRef, useState } from "react";
import { BrandMark } from "../../../shared/ui";
import { mountGalaxyOrb } from "./galaxyRenderer";
import "./orb.css";

export function GalaxyOrb(): React.ReactElement {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    if (!canvas.current) return;
    return mountGalaxyOrb(canvas.current, () => setUnavailable(true));
  }, []);

  return <div className="lunmi-galaxy-orb" data-testid="chat-orb" data-unavailable={unavailable}
    role="img" aria-label="Lunmi">
    <canvas ref={canvas} aria-hidden="true" hidden={unavailable} />
    {unavailable && <BrandMark size={84} />}
  </div>;
}
