// Single source of truth for the semantic signal palette. The three tier
// hexes were previously duplicated in api.ts (TIER_COLOR), BrandLogo.tsx and
// RequestDrilldown.tsx — everything now reads from here. Kept in sync by hand
// with the Tailwind tokens in tailwind.config.js (Tailwind classes vs. inline
// SVG/Recharts hex are two consumers of the same palette).
export const TIER_HEX = {
  cheap: "#35D6A0",
  mid: "#4C8DFF",
  frontier: "#F5A524",
} as const;

export const SIGNAL_HEX = {
  privacy: "#8B7BF7", // residency dimension (not a tier)
  alert: "#F2555A", // breaches / health-fail ONLY
} as const;

export type Tier = keyof typeof TIER_HEX;
