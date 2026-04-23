/**
 * Accuracy streak computation.
 *
 * A "win" is when settlement_outcome === side.
 * Walks settled positions sorted by settled_at descending to find
 * the current active streak, and scans fully for the best-ever streak.
 */

import type { SettledPosition } from "@/hooks/use-dashboard-data";

function isWin(p: SettledPosition): boolean {
  return p.settlement_outcome != null && p.settlement_outcome === p.side;
}

function sortedBySettledDesc(positions: SettledPosition[]): SettledPosition[] {
  return [...positions].sort((a, b) => {
    const ta = a.settled_at ? new Date(a.settled_at).getTime() : 0;
    const tb = b.settled_at ? new Date(b.settled_at).getTime() : 0;
    return tb - ta;
  });
}

/** Current active win streak (from most recent settled backward). */
export function computeCurrentStreak(settled: SettledPosition[]): number {
  const sorted = sortedBySettledDesc(settled);
  let streak = 0;
  for (const p of sorted) {
    if (!isWin(p)) break;
    streak++;
  }
  return streak;
}

/** Best-ever win streak across all settled positions. */
export function computeBestStreak(settled: SettledPosition[]): number {
  // Walk chronologically for best streak
  const sorted = [...settled].sort((a, b) => {
    const ta = a.settled_at ? new Date(a.settled_at).getTime() : 0;
    const tb = b.settled_at ? new Date(b.settled_at).getTime() : 0;
    return ta - tb;
  });

  let best = 0;
  let current = 0;
  for (const p of sorted) {
    if (isWin(p)) {
      current++;
      best = Math.max(best, current);
    } else {
      current = 0;
    }
  }
  return best;
}

/** Icon for streak display - escalates with streak length. */
export function streakIcon(count: number): string {
  if (count >= 10) return "⚡";
  if (count >= 5) return "🔥";
  if (count >= 3) return "✨";
  return "-";
}
