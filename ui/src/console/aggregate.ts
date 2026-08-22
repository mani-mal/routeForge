import type { RequestSummary } from "../api";

export function percentile(sorted: number[], p: number): number {
  if (sorted.length === 0) return 0;
  const idx = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[idx];
}

export function costAgg(rs: RequestSummary[]) {
  const total = rs.reduce((a, r) => a + r.total_usd, 0);
  const baseline = rs.reduce((a, r) => a + r.baseline_usd, 0);
  const routingSaved = rs.reduce((a, r) => a + r.routing_saved_usd, 0);
  const compressionSaved = rs.reduce((a, r) => a + r.compression_saved_usd, 0);
  const savedPct = baseline > 0 ? ((baseline - total) / baseline) * 100 : 0;
  return { total, baseline, routingSaved, compressionSaved, savedPct };
}

// Quality-per-resource density, mirroring the backend formulas in
// app/api/history.py's HistoryStore.stats()/fleet(): quality summed across
// calls, divided by tokens (x1000, "per 1k tokens") or by USD spend. This
// section is per-app (tenant-filtered `requests`), so density is derived
// client-side from those same rows rather than the cross-tenant `/api/stats`
// endpoint, keeping every Cost-tab figure scoped to the one application.
export function densityAgg(rs: RequestSummary[]) {
  const qualitySum = rs.reduce((a, r) => a + r.quality_sum, 0);
  const tokenSum = rs.reduce((a, r) => a + r.total_tokens, 0);
  const usdSum = rs.reduce((a, r) => a + r.total_usd, 0);
  const tokenIntelligenceDensity = tokenSum > 0 ? (qualitySum / tokenSum) * 1000 : 0;
  const costIntelligenceDensity = usdSum > 0 ? qualitySum / usdSum : 0;
  return { tokenIntelligenceDensity, costIntelligenceDensity };
}

// Provenance distribution for the quality axis — a Counter over quality_source
// (tier_default | calibrated | published | customer; app/routing/quality.py),
// mirroring the backend's quality_source_distribution so the honesty caveat
// can cite it client-side for the (per-app, tenant-filtered) `requests` rows.
export function qualitySourceAgg(rs: RequestSummary[]): Record<string, number> {
  const dist: Record<string, number> = {};
  for (const r of rs) {
    if (r.quality_source) dist[r.quality_source] = (dist[r.quality_source] ?? 0) + 1;
  }
  return dist;
}

export function privacyAgg(rs: RequestSummary[]) {
  const withPii = rs.filter((r) => r.detected_entities.length > 0);
  const onprem = rs.filter((r) => r.onprem_only);
  const entityTypes = [...new Set(rs.flatMap((r) => r.detected_entities))].sort();
  // "kept off cloud" = sensitive requests that were forced on-prem (the privacy proof)
  const keptOffCloud = rs.filter((r) => r.onprem_only && r.detected_entities.length > 0).length;
  return { pii: withPii.length, onprem: onprem.length, keptOffCloud, entityTypes };
}

export function trafficAgg(rs: RequestSummary[]) {
  const tierMix: Record<string, number> = {};
  for (const r of rs) tierMix[r.tier] = (tierMix[r.tier] ?? 0) + 1;
  const lat = rs.map((r) => r.latency_ms).sort((a, b) => a - b);
  const multiStep = rs.filter((r) => r.hops > 1).length;
  return {
    volume: rs.length,
    tierMix,
    p50: percentile(lat, 50),
    p95: percentile(lat, 95),
    multiStepPct: rs.length ? (multiStep / rs.length) * 100 : 0,
  };
}
