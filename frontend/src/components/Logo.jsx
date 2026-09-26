import { useId } from "react";

/** The HireRizz mark: an "H" whose crossbar is a check (hired), with a spark. */
export function LogoMark({ size = 30, animated = true }) {
  const id = useId().replace(/:/g, "");
  return (
    <svg className={`logo-mark${animated ? " animated" : ""}`} width={size} height={size} viewBox="0 0 40 40" aria-hidden="true">
      <defs>
        <linearGradient id={`g${id}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#6d5efc" />
          <stop offset="0.55" stopColor="#a855f7" />
          <stop offset="1" stopColor="#ec4899" />
          {animated && (
            <animateTransform attributeName="gradientTransform" type="rotate" values="0 .5 .5;360 .5 .5" dur="9s" repeatCount="indefinite" />
          )}
        </linearGradient>
        <radialGradient id={`s${id}`} cx="0.3" cy="0.2" r="0.9">
          <stop offset="0" stopColor="#fff" stopOpacity="0.45" />
          <stop offset="0.6" stopColor="#fff" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect x="1" y="1" width="38" height="38" rx="11" fill={`url(#g${id})`} />
      <rect x="1" y="1" width="38" height="38" rx="11" fill={`url(#s${id})`} />
      <rect x="8.5" y="9.5" width="4.6" height="21" rx="2.3" fill="#fff" />
      <rect x="21.4" y="9.5" width="4.6" height="21" rx="2.3" fill="#fff" />
      <rect x="10.5" y="17.7" width="13.5" height="4.6" rx="2.3" fill="#fff" />
      <g className="logo-badge">
        <circle cx="30.6" cy="29.8" r="7" fill="#fff" />
        <path className="logo-check" d="M27.5 30 L29.8 32.3 L33.9 27.9" fill="none" stroke="#8b5cf6" strokeWidth="2.4"
          strokeLinecap="round" strokeLinejoin="round" />
      </g>
      <path className="logo-spark" d="M33 3.2 L34.1 6.4 L37.3 7.5 L34.1 8.6 L33 11.8 L31.9 8.6 L28.7 7.5 L31.9 6.4 Z" fill="#fde68a" />
    </svg>
  );
}

export function Logo({ size = 30, onClick, href = "/" }) {
  return (
    <a className="brand" href={href} onClick={onClick}>
      <LogoMark size={size} />
      <span className="wordmark">Hire<span className="grad-text">Rizz</span></span>
    </a>
  );
}
