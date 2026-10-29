import { useEffect, useRef, type MutableRefObject } from "react";

export type OrbState = "idle" | "listening" | "thinking" | "responding" | "speaking" | "offline";

interface Look {
  hue: number; // HSL hue of the ring
  energy: number; // base wobble
  spin: number; // rotation speed (rad/s)
  glow: number;
}

const LOOKS: Record<OrbState, Look> = {
  idle: { hue: 214, energy: 0.05, spin: 0.08, glow: 0.35 },
  listening: { hue: 188, energy: 0.2, spin: 0.15, glow: 0.7 },
  thinking: { hue: 262, energy: 0.1, spin: 1.1, glow: 0.55 },
  responding: { hue: 205, energy: 0.12, spin: 0.3, glow: 0.6 },
  speaking: { hue: 172, energy: 0.22, spin: 0.2, glow: 0.8 },
  offline: { hue: 38, energy: 0.03, spin: 0.04, glow: 0.3 },
};

const POINTS = 120;

/**
 * The assistant's "core": three concentric rings whose shape follows what NEXUS is doing.
 * Listening: rings swell with the real microphone level. Thinking: rings counter-rotate.
 * Speaking: rings pulse. Respects prefers-reduced-motion by drawing a still frame.
 */
export function Orb({ state, levelRef, size = 340 }: { state: OrbState; levelRef: MutableRefObject<number>; size?: number }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const stateRef = useRef(state);
  stateRef.current = state;
  const reduced = typeof window !== "undefined" && (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false);

  const redrawKey = reduced ? state : "live"; // reduced motion: redraw the still frame when the state changes

  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d");
    if (!el || !ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    el.width = size * dpr;
    el.height = size * dpr;
    ctx.scale(dpr, dpr);

    // Smoothed look so state changes glide instead of snapping.
    const cur = { ...LOOKS[stateRef.current] };
    let amp = 0;
    let phase = 0;
    let last = performance.now();
    let raf = 0;

    const draw = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const target = LOOKS[stateRef.current];
      const k = 1 - Math.exp(-dt * 4);
      cur.hue += (target.hue - cur.hue) * k;
      cur.energy += (target.energy - cur.energy) * k;
      cur.spin += (target.spin - cur.spin) * k;
      cur.glow += (target.glow - cur.glow) * k;

      const t = now / 1000;
      const s = stateRef.current;
      let drive = 0;
      if (s === "listening") drive = levelRef.current;
      else if (s === "speaking") drive = 0.45 + 0.35 * Math.sin(t * 7.3) * Math.sin(t * 2.1 + 1) + 0.15 * Math.sin(t * 13);
      else if (s === "responding") drive = 0.25 + 0.15 * Math.sin(t * 5);
      amp += (Math.max(0, drive) - amp) * (1 - Math.exp(-dt * 12));
      phase += dt * cur.spin;

      const c = size / 2;
      const base = size * 0.27;
      ctx.clearRect(0, 0, size, size);

      // Soft core glow.
      const g = ctx.createRadialGradient(c, c, 0, c, c, size * 0.5);
      g.addColorStop(0, `hsla(${cur.hue}, 90%, 62%, ${0.22 * cur.glow + amp * 0.25})`);
      g.addColorStop(0.55, `hsla(${cur.hue}, 85%, 55%, ${0.07 * cur.glow})`);
      g.addColorStop(1, "hsla(0,0%,0%,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, size, size);

      for (let ring = 0; ring < 3; ring++) {
        const dir = ring % 2 === 0 ? 1 : -1;
        const radius = base * (1 + ring * 0.2) + amp * size * (0.07 - ring * 0.015);
        const wob = (cur.energy + amp * 0.35) * base * (0.16 + ring * 0.05);
        ctx.beginPath();
        for (let i = 0; i <= POINTS; i++) {
          const a = (i / POINTS) * Math.PI * 2;
          const n =
            Math.sin(a * 3 + phase * dir * 2 + ring) * 0.6 +
            Math.sin(a * 5 - t * (0.6 + ring * 0.25) * dir) * 0.3 +
            Math.sin(a * 9 + t * 1.3 + ring * 2) * 0.1;
          const r = radius + n * wob;
          const x = c + Math.cos(a + phase * dir) * r;
          const y = c + Math.sin(a + phase * dir) * r;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.closePath();
        const alpha = (0.85 - ring * 0.25) * (0.55 + cur.glow * 0.45);
        ctx.strokeStyle = `hsla(${cur.hue + ring * 42}, 95%, ${62 - ring * 2}%, ${alpha})`;
        ctx.lineWidth = ring === 0 ? 2.8 : 1.8;
        ctx.shadowColor = `hsla(${cur.hue}, 100%, 60%, ${0.8 * cur.glow})`;
        ctx.shadowBlur = ring === 0 ? 18 : 8;
        ctx.stroke();
      }
      ctx.shadowBlur = 0;
      if (!reduced) raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [size, levelRef, reduced, redrawKey]);

  return <canvas ref={canvas} style={{ width: size, height: size }} className="max-w-full" aria-hidden />;
}
