// Color theme — dark by default, persisted to localStorage and applied as a
// `.light` / `.dark` class on <html>. A tiny event so any component (Recharts
// needs concrete colours, not CSS vars) re-renders when the theme flips.
import { useEffect, useState } from "react";

export type Theme = "dark" | "light";

const KEY = "rf-theme";
const EVENT = "rf-theme-change";

export function getTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(theme: Theme): void {
  const el = document.documentElement;
  el.classList.toggle("light", theme === "light");
  el.classList.toggle("dark", theme === "dark");
  el.style.colorScheme = theme;
}

export function setTheme(theme: Theme): void {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* private mode — still apply for this session */
  }
  applyTheme(theme);
  window.dispatchEvent(new CustomEvent(EVENT, { detail: theme }));
}

export function toggleTheme(): Theme {
  const next: Theme = getTheme() === "dark" ? "light" : "dark";
  setTheme(next);
  return next;
}

// Reactive accessor: returns [theme, toggle] and re-renders on any theme change.
export function useTheme(): [Theme, () => void] {
  const [theme, setThemeState] = useState<Theme>(getTheme);
  useEffect(() => {
    const handler = () => setThemeState(getTheme());
    window.addEventListener(EVENT, handler);
    return () => window.removeEventListener(EVENT, handler);
  }, []);
  return [theme, toggleTheme];
}
