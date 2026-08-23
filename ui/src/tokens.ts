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
  // Quality-proxy axis (Token/Cost Intelligence Density, cost-vs-quality
  // chart). Deliberately its own hue — distinct from privacy's violet and
  // every cost/tier color — because it's a different signal: quality is a
  // tier-quality PROXY (or calibrated/customer when set), never measured
  // accuracy. See app/routing/quality.py's provenance ladder.
  quality: "#EC4899",
} as const;

// The two cost-savings levers (routing vs. compression) shown side-by-side in
// CostSection's "savings by lever" chart. Reuses existing tier hexes rather
// than inventing new ones: `mid` blue for routing keeps it far from the
// privacy violet (#8B7BF7) it used to collide with as a near-duplicate
// #8b5cf6; `frontier` amber for compression is the pre-existing amber in
// this palette. Neither lever is itself a tier — this is a distinct mapping.
export const LEVER_HEX = {
  routing: TIER_HEX.mid,
  compression: TIER_HEX.frontier,
} as const;

export type Tier = keyof typeof TIER_HEX;
