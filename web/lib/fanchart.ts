import type { SimPath, WeeklyBand } from "./types";

export type { WeeklyBand };

function percentile(sorted: number[], p: number): number {
  if (sorted.length === 1) return sorted[0];
  const idx = (sorted.length - 1) * p;
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return sorted[lo];
  const frac = idx - lo;
  return sorted[lo] + (sorted[hi] - sorted[lo]) * frac;
}

// Computes a p10/p50/p90 band per week from the full path set. SimResult ships
// full per-path balances, so the band is derived here in the browser.
export function computeWeeklyBand(paths: SimPath[]): WeeklyBand[] {
  if (paths.length === 0) return [];
  const weeks = Math.max(...paths.map((p) => p.balances.length));
  const band: WeeklyBand[] = [];

  for (let w = 0; w < weeks; w++) {
    const values = paths
      .map((p) => p.balances[w])
      .filter((v): v is number => typeof v === "number" && Number.isFinite(v))
      .sort((a, b) => a - b);
    if (values.length === 0) continue;
    band.push({
      week: w,
      p10: percentile(values, 0.1),
      p50: percentile(values, 0.5),
      p90: percentile(values, 0.9),
    });
  }

  return band;
}

// Picks an evenly spaced subset of paths to draw as thin individual lines
// on top of the band, so we never render all 500 polylines.
export function sampleRepresentativePaths(paths: SimPath[], count: number): SimPath[] {
  const ranked = [...paths].sort((a, b) => a.min_balance - b.min_balance || a.path_id - b.path_id);
  const total = Math.min(Math.max(Math.floor(count), 0), ranked.length);
  return Array.from({ length: total }, (_, i) => ranked[total === 1 ? 0 : Math.round(i * (ranked.length - 1) / (total - 1))]);
}
