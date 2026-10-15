import React, { useEffect, useMemo, useRef } from "react";
import { version } from "../../../package.json";
import { TOKENS } from "../../shared/theme";
import "./startup.css";

// Last entrance finishes at 3.9s + 1.6s, matching the supplied animation.
export const INTRO_DURATION_MS = 5500;
const ENTRANCE_WAVE = [
  [.7, .95, -20, 200, 17], [.95, .78, -40, 230, 16.5],
  [1.15, 40, .12, 190, 16], [1.35, .65, -30, 220, 17.5],
  [1.55, .88, .05, 240, 18], [1.75, .52, -50, 180, 16],
  [1.95, 50, .26, 250, 18], [2.15, .90, -10, 380, 18.5],
  [2.4, .98, .18, 210, 17], [2.65, .72, -30, 230, 17.5],
  [2.9, 40, .40, 200, 16.5], [3.15, .60, -40, 240, 18],
] as const;

interface Meteor {
  x: number; y: number; dx: number; dy: number; speed: number;
  tail: number; head: number; hero: boolean; opacity: number;
}

// Time-based motion keeps the supplied southwest trajectory identical at 60/120 Hz.
export function advanceMeteor(m: Meteor, seconds: number): void {
  m.x += m.dx * m.speed * seconds;
  m.y += m.dy * m.speed * seconds;
  m.opacity = Math.min(1, m.opacity + seconds * 4.8);
}

function paintMeteor(ctx: CanvasRenderingContext2D, m: Meteor): void {
  const tx = m.x - m.dx * m.tail, ty = m.y - m.dy * m.tail;
  ctx.save(); ctx.globalAlpha = m.opacity;
  const tail = ctx.createLinearGradient(m.x, m.y, tx, ty);
  tail.addColorStop(0, "rgba(255,255,255,1)");
  tail.addColorStop(.12, "rgba(215,235,255,.95)");
  tail.addColorStop(.35, "rgba(150,195,255,.6)");
  tail.addColorStop(.7, "rgba(99,140,255,.2)");
  tail.addColorStop(1, "rgba(99,140,255,0)");
  ctx.beginPath(); ctx.moveTo(m.x, m.y); ctx.lineTo(tx, ty);
  ctx.strokeStyle = tail; ctx.lineWidth = m.hero ? 2.6 : m.head * .75;
  ctx.lineCap = "round"; ctx.stroke();
  const radius = m.hero ? 24 : m.head * 4.8;
  const glow = ctx.createRadialGradient(m.x, m.y, 0, m.x, m.y, radius);
  glow.addColorStop(0, "rgba(255,255,255,.95)");
  glow.addColorStop(.35, "rgba(180,215,255,.5)");
  glow.addColorStop(.7, "rgba(110,150,255,.2)");
  glow.addColorStop(1, "rgba(99,135,255,0)");
  ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(m.x, m.y, radius, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = "#ffffff"; ctx.shadowColor = "#ffffff"; ctx.shadowBlur = m.hero ? 14 : 7;
  ctx.beginPath(); ctx.arc(m.x, m.y, m.head, 0, Math.PI * 2); ctx.fill(); ctx.restore();
}

export function StartupScreen({ message, error, onRetry, leaving = false }: {
  message: string; error: boolean; onRetry: () => void; leaving?: boolean;
}): React.ReactElement {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stars = useMemo(() => Array.from({ length: 85 }, (_, i) => ({
    left: `${Math.random() * 100}%`, top: `${Math.random() * 100}%`,
    width: i % 4 ? 1.4 : 2.7, height: i % 4 ? 1.4 : 2.7,
    animationDelay: `${-(i % 40) / 10}s`, animationDuration: `${2.4 + i % 32 / 10}s`,
  })), []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let width = 0, height = 0, frame = 0, last = 0, elapsed = 0, nextSpawn = .7, wave = 0;
    let meteors: Meteor[] = [];
    function resize() {
      const rect = canvas!.getBoundingClientRect(); width = rect.width; height = rect.height;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas!.width = Math.round(width * dpr); canvas!.height = Math.round(height * dpr);
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    function render(time: number) {
      const dt = last ? Math.min((time - last) / 1000, .05) : 0; last = time; elapsed += dt;
      if (elapsed >= nextSpawn && (wave < ENTRANCE_WAVE.length || meteors.length < 9)) {
        const angle = (218 + Math.random() * 4) * Math.PI / 180;
        const preset = ENTRANCE_WAVE[wave];
        const hero = preset ? wave === 7 : Math.random() > .88;
        const x = preset ? (preset[1] > 1 ? width + preset[1] : width * preset[1]) : width * (.4 + Math.random() * .65);
        const y = preset ? (preset[2] < 0 ? preset[2] : height * preset[2]) : -60;
        meteors.push({ x, y,
          dx: Math.cos(angle), dy: -Math.sin(angle), speed: (preset?.[4] ?? (15 + Math.random() * 5)) * 60,
          tail: preset?.[3] ?? (hero ? 360 : 190 + Math.random() * 110), head: hero ? 4.2 : 2.5 + Math.random(), hero, opacity: 0 });
        wave++;
        nextSpawn = ENTRANCE_WAVE[wave]?.[0] ?? (wave === 12 ? 3.4 : elapsed + 1.1 + Math.random() * 1.3);
      }
      ctx!.clearRect(0, 0, width, height);
      for (const m of meteors) { advanceMeteor(m, dt); paintMeteor(ctx!, m); }
      meteors = meteors.filter(m => m.x > -m.tail * 1.5 && m.y < height + m.tail * 1.5);
      frame = requestAnimationFrame(render);
    }
    function syncMotion() {
      cancelAnimationFrame(frame); frame = 0; last = 0;
      ctx!.clearRect(0, 0, width, height);
      if (!document.hidden && !motion.matches) frame = requestAnimationFrame(render);
    }
    resize(); syncMotion();
    window.addEventListener("resize", resize);
    document.addEventListener("visibilitychange", syncMotion);
    motion.addEventListener("change", syncMotion);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", resize);
      document.removeEventListener("visibilitychange", syncMotion);
      motion.removeEventListener("change", syncMotion);
    };
  }, []);

  return <section className="lunmi-startup" data-testid="startup-screen" data-leaving={leaving} aria-label="Inicialização da Lunmi">
    <div className="startup-atmosphere" aria-hidden="true" />
    <div className="startup-stars" aria-hidden="true">{stars.map((style, i) => <i key={i} style={style} />)}</div>
    <canvas ref={canvasRef} className="startup-meteors" aria-hidden="true" />
    <header className="startup-header"><span><i aria-hidden="true" />Desktop AI</span><span>v{version}</span></header>
    <main className="startup-lockup">
      <h1>LUNMI</h1><p className="startup-subtitle">Intelligent Desktop Companion</p>
      <div className={`startup-status ${error ? "startup-error" : ""}`} role={error ? "alert" : "status"}>
        <i aria-hidden="true" style={{ background: error ? TOKENS.error : TOKENS.accent }} /><span>{message}</span>
      </div>
      {error && <button type="button" className="startup-retry" onClick={onRetry}>Tentar novamente</button>}
    </main>
  </section>;
}
