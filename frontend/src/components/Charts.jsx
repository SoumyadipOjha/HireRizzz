import { useEffect, useRef, useState } from "react";

const reduced = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** true one frame after mount: CSS transitions then animate from their start values. */
export function useMounted(delay = 60) {
  const [on, setOn] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setOn(true), reduced() ? 0 : delay);
    return () => clearTimeout(t);
  }, [delay]);
  return on;
}

/** Animated number that counts up to `value`. */
export function CountUp({ value, decimals = 0, duration = 1100, suffix = "" }) {
  const [v, setV] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    if (value == null) return undefined;
    if (reduced()) {
      setV(value);
      return undefined;
    }
    const start = performance.now(), a = from.current, b = Number(value);
    let raf;
    const tick = (t) => {
      const p = Math.min(1, (t - start) / duration);
      const e = 1 - Math.pow(1 - p, 4);
      setV(a + (b - a) * e);
      if (p < 1) raf = requestAnimationFrame(tick);
      else from.current = b;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);
  if (value == null) return <>—</>;
  return <>{v.toFixed(decimals)}{suffix}</>;
}

// ------------------------------------------------------------------ donut

export function Donut({ segments, size = 190, thickness = 24, centerLabel = "total", onSelect }) {
  const mounted = useMounted(120);
  const [hover, setHover] = useState(null);
  const total = segments.reduce((s, x) => s + x.value, 0);
  const r = (size - thickness) / 2 - 4;
  const C = 2 * Math.PI * r;
  const shown = segments.filter((s) => s.value > 0);
  const gap = shown.length > 1 ? 3 : 0;
  let acc = 0;
  const active = hover != null ? segments.find((s) => s.key === hover) : null;

  return (
    <div className="donut-wrap">
      <div className="donut" style={{ width: size, height: size }}>
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-2)" strokeWidth={thickness} />
          <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
            {shown.map((s) => {
              const len = total ? (s.value / total) * C : 0;
              const dash = Math.max(0, len - gap);
              const off = acc;
              acc += len;
              const on = hover === s.key;
              return (
                <circle key={s.key} cx={size / 2} cy={size / 2} r={r} fill="none" stroke={s.color}
                  strokeWidth={on ? thickness + 8 : thickness} strokeLinecap="butt"
                  strokeDasharray={`${mounted ? dash : 0} ${C}`} strokeDashoffset={-off}
                  className="donut-seg" style={{ opacity: hover && !on ? 0.35 : 1 }}
                  onMouseEnter={() => setHover(s.key)} onMouseLeave={() => setHover(null)}
                  onClick={() => onSelect?.(s)} />
              );
            })}
          </g>
        </svg>
        <div className="donut-center">
          <b><CountUp value={active ? active.value : total} /></b>
          <span>{active ? active.label : centerLabel}</span>
          {active && total > 0 && <em>{Math.round((active.value / total) * 100)}%</em>}
        </div>
      </div>
      <div className="legend">
        {segments.map((s) => (
          <button key={s.key} className={`legend-item${hover === s.key ? " on" : ""}`}
            onMouseEnter={() => setHover(s.key)} onMouseLeave={() => setHover(null)} onClick={() => onSelect?.(s)}>
            <span className="swatch" style={{ background: s.color }} />
            <span className="grow">{s.label}</span>
            <b>{s.value}</b>
          </button>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ semicircle gauge

export function Gauge({ value, label, sub }) {
  const mounted = useMounted(200);
  const pct = value == null ? 0 : Math.max(0, Math.min(100, value));
  const R = 70, C = Math.PI * R;
  return (
    <div className="gauge">
      <svg viewBox="0 0 180 104" width="100%">
        <defs>
          <linearGradient id="gaugegrad" x1="0" x2="1">
            <stop offset="0" stopColor="#6d5efc" />
            <stop offset="0.6" stopColor="#a855f7" />
            <stop offset="1" stopColor="#ec4899" />
          </linearGradient>
        </defs>
        <path d={`M ${90 - R} 94 A ${R} ${R} 0 0 1 ${90 + R} 94`} fill="none" stroke="var(--surface-2)" strokeWidth="16" strokeLinecap="round" />
        <path d={`M ${90 - R} 94 A ${R} ${R} 0 0 1 ${90 + R} 94`} fill="none" stroke="url(#gaugegrad)" strokeWidth="16" strokeLinecap="round"
          strokeDasharray={`${mounted ? (pct / 100) * C : 0} ${C}`} className="gauge-arc" />
      </svg>
      <div className="gauge-center">
        <b>{value == null ? "—" : <CountUp value={pct} suffix="%" />}</b>
        <span>{label}</span>
      </div>
      {sub && <div className="gauge-sub">{sub}</div>}
    </div>
  );
}
