/**
 * Profit Score — composite metric (0–100), weighted toward profitability.
 *
 * The user's #1 goal is making money, not being "accurate". Weight the
 * components accordingly: ROI dominates, calibration is a smaller sanity
 * check, win rate is a minor contributor (a bot can win 80% of the time
 * and still lose money if winners are much smaller than losers).
 *
 *   ROI         (60 points)  — realized P&L / starting capital
 *   Brier       (25 points)  — lower is better (0.00 → 25, 0.25 → 0)
 *   Win rate    (15 points)  — directional tie-breaker
 *
 * Final score clamped [0, 100].
 */

function clamp(v: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, v));
}

export function computeProfitScore(
  brier: number | null,
  winRate: number | null,
  roiPct: number | null,
): number {
  // ROI component (60 points): +100% → 60, -100% → -60, clamped
  const roiPts = roiPct != null ? clamp(roiPct, -1, 1) * 60 : 0;

  // Brier component (25 points): 0.0 → 25, 0.25 → 0
  const brierPts = brier != null ? (1 - brier / 0.25) * 25 : 0;

  // Win rate component (15 points): 1.0 → 15, 0.0 → 0
  const winPts = winRate != null ? winRate * 15 : 0;

  return Math.round(clamp(roiPts + brierPts + winPts, 0, 100));
}

export function profitLabel(score: number): string {
  if (score >= 75) return "Excellent";
  if (score >= 55) return "Good";
  if (score >= 35) return "Fair";
  return "Developing";
}

export function profitColor(score: number): string {
  if (score >= 75) return "text-accent";
  if (score >= 55) return "text-accent";
  if (score >= 35) return "text-amber-400";
  return "text-red-400";
}

export function profitStrokeColor(score: number): string {
  if (score >= 75) return "#10b981";
  if (score >= 55) return "#34d399";
  if (score >= 35) return "#f59e0b";
  return "#ef4444";
}
