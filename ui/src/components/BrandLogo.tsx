// Brand + chrome marks for the header.
//
// The routeForge mark depicts the product itself: one incoming request routed
// to three tier endpoints — cheap (green), mid (blue), frontier (amber) — reusing
// the exact tier palette the rest of the UI uses (tokens.ts), so the logo *is*
// the router. The trunk uses currentColor, so it follows the active theme's accent.
import cdwLogo from "../assets/cdw-logo.png";
import { TIER_HEX } from "../tokens";

export function RouteForgeMark({ className = "h-7 w-7" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} role="img" aria-label="routeForge">
      <g fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round">
        <path d="M6 16h7" />
        <path d="M13 16c5 0 5-8 11-8" />
        <path d="M13 16h11" />
        <path d="M13 16c5 0 5 8 11 8" />
      </g>
      <circle cx="6" cy="16" r="2.6" fill="currentColor" />
      <circle cx="25" cy="8" r="3" fill={TIER_HEX.cheap} />
      <circle cx="25" cy="16" r="3" fill={TIER_HEX.mid} />
      <circle cx="25" cy="24" r="3" fill={TIER_HEX.frontier} />
    </svg>
  );
}

export function RouteForgeLogo() {
  return (
    <div className="flex items-center gap-2 select-none">
      <span className="text-accent">
        <RouteForgeMark />
      </span>
      <span className="text-xl font-display font-bold tracking-tight text-gray-100">
        route<span className="text-accent">Forge</span>
      </span>
    </div>
  );
}

export function CdwMark({ className = "h-5 w-auto" }: { className?: string }) {
  return <img src={cdwLogo} alt="CDW" className={className} />;
}

export function ThemeToggle({ theme, onToggle }: { theme: string; onToggle: () => void }) {
  const isDark = theme === "dark";
  return (
    <button
      type="button"
      onClick={onToggle}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      aria-label="Toggle color theme"
      className="flex h-8 w-8 items-center justify-center rounded-md border border-edge text-gray-400 hover:text-accent hover:border-accent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      {isDark ? (
        <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
        </svg>
      ) : (
        <svg
          viewBox="0 0 24 24"
          className="h-4 w-4"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </svg>
      )}
    </button>
  );
}
