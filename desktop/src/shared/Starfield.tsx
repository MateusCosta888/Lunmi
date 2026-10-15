// Adapted from Aceternity StarsBackground + ShootingStars. Sources/license: THIRD-PARTY-MOTION.md.
import React, { useEffect, useRef } from "react";
import { getTheme } from "./theme";
import { useVisualActivity } from "./useVisualActivity";

export function Starfield({ paused = false, themeId = "celestial" }: { paused?: boolean; themeId?: string }): React.ReactElement {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const { reduced, visible } = useVisualActivity();
  useEffect(() => {
    const canvas = canvasRef.current;
    const colors = getTheme(themeId).colors;
    const context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    let width = 0, height = 0, frame = 0, previous = 0, nextMeteor = 0;
    let stars: Array<{ x: number; y: number; radius: number; phase: number; period: number }> = [];
    let meteor: { x: number; y: number; vx: number; vy: number; distance: number } | null = null;
    const active = !paused && !reduced && visible;
    const paint = (time: number, delta: number) => {
      context.clearRect(0, 0, width, height);
      context.fillStyle = colors.textSecondary;
      for (const star of stars) {
        context.globalAlpha = active ? 0.22 + Math.abs(Math.sin(time / star.period + star.phase)) * 0.38 : 0.38;
        context.beginPath(); context.arc(star.x, star.y, star.radius, 0, Math.PI * 2); context.fill();
      }
      context.globalAlpha = 1;
      if (!active) return;
      if (!meteor && time >= nextMeteor) {
        const speed = 340 + Math.random() * 150;
        meteor = { x: width * (0.4 + Math.random() * 0.6), y: -25, vx: -speed * 0.78, vy: speed * 0.62, distance: 0 };
        nextMeteor = time + 4200 + Math.random() * 4500;
      }
      if (meteor) {
        meteor.x += meteor.vx * delta; meteor.y += meteor.vy * delta; meteor.distance += Math.hypot(meteor.vx, meteor.vy) * delta;
        const length = Math.min(170, 35 + meteor.distance * 0.22);
        const tailX = meteor.x + length * 0.78, tailY = meteor.y - length * 0.62;
        const gradient = context.createLinearGradient(tailX, tailY, meteor.x, meteor.y);
        gradient.addColorStop(0, "transparent"); gradient.addColorStop(0.8, colors.accent); gradient.addColorStop(1, colors.textPrimary);
        context.globalAlpha = 0.3; context.strokeStyle = gradient; context.lineWidth = 1.2;
        context.beginPath(); context.moveTo(tailX, tailY); context.lineTo(meteor.x, meteor.y); context.stroke();
        context.globalAlpha = 1;
        if (meteor.x < -length || meteor.y > height + length) meteor = null;
      }
    };
    const resize = () => {
      const rect = canvas.getBoundingClientRect(); width = rect.width; height = rect.height;
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      stars = Array.from({ length: Math.min(180, Math.floor(width * height * 0.00015)) }, () => ({
        x: Math.random() * width, y: Math.random() * height, radius: 0.4 + Math.random() * 0.45,
        phase: Math.random() * Math.PI * 2, period: 2400 + Math.random() * 3600,
      }));
      paint(performance.now(), 0);
    };
    const tick = (time: number) => {
      if (!previous || time - previous >= 1000 / 30) {
        paint(time, previous ? Math.min(0.05, (time - previous) / 1000) : 0);
        previous = time;
      }
      frame = requestAnimationFrame(tick);
    };
    resize(); nextMeteor = performance.now() + 1500;
    const observer = new ResizeObserver(resize); observer.observe(canvas);
    if (active) frame = requestAnimationFrame(tick);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [paused, reduced, visible, themeId]);
  return <div className="lunmi-cosmos" aria-hidden="true" data-testid="starfield" data-paused={paused || reduced || !visible}>
    <canvas ref={canvasRef} /><div className="lunmi-cosmos-shade" />
  </div>;
}
