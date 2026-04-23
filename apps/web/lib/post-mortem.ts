/**
 * Post-mortem analysis for losing trades.
 *
 * Transforms a raw LOSS into a structured "recalibration event"
 * that frames the loss as a learning opportunity.
 */

import type { SettledPosition } from "@/hooks/use-dashboard-data";

export type PostMortemData = {
  position: SettledPosition;
  predictionError: number | null;
  overconfident: boolean;
  narrative: string;
  /** Diagnostic for a specific failure pattern; null when no signal fired. */
  lesson: string | null;
};

export function buildPostMortem(p: SettledPosition): PostMortemData {
  const won = p.settlement_outcome === p.side;
  // claude_probability is always P(YES). Compare against the actual
  // outcome (1.0 for YES, 0.0 for NO) - NOT settlement_price, which
  // is the token payout (0/1 depending on which side the bot held).
  const actualOutcome =
    p.settlement_outcome === "YES" ? 1.0 : p.settlement_outcome === "NO" ? 0.0 : null;
  const predictionError =
    p.claude_probability != null && actualOutcome != null
      ? Math.abs(p.claude_probability - actualOutcome)
      : null;

  const overconfident =
    p.confidence != null && p.confidence >= 0.7 && !won;

  // Build narrative
  const sideStr = p.side;
  const probStr =
    p.claude_probability != null
      ? `${(p.claude_probability * 100).toFixed(1)}%`
      : "unknown";
  const outcome = p.settlement_outcome ?? "unknown";

  // Infer the market's YES probability from the entry price + side
  const marketYesPct =
    p.side === "YES"
      ? (p.entry_price * 100).toFixed(0)
      : ((1 - p.entry_price) * 100).toFixed(0);

  let narrative: string;
  if (p.side === "YES") {
    narrative = `Bought YES at $${p.entry_price.toFixed(3)} (market ${marketYesPct}% YES), estimating ${probStr} YES probability. Market resolved ${outcome}.`;
  } else {
    narrative = `Bought NO at $${p.entry_price.toFixed(3)} (market ${marketYesPct}% YES), estimating only ${probStr} YES probability. Market resolved ${outcome}.`;
  }

  // Build lesson - only fires when a specific diagnostic pattern matches.
  // We deliberately do NOT emit a catch-all "just variance" reassurance
  // for losses that don't trigger any of these: losing money should not
  // read like a feature, and stringing together enough "standard variance"
  // losses is exactly how a bad strategy bleeds the bankroll in silence.
  let lesson: string | null = null;
  if (predictionError != null && predictionError > 0.3) {
    lesson = `Large calibration miss: ${(predictionError * 100).toFixed(0)}pp error between the bot's probability and the actual outcome.`;
  } else if (overconfident) {
    lesson = `High-confidence loss (${((p.confidence ?? 0) * 100).toFixed(0)}% confidence, wrong side). Watch for similar ${p.category ?? "market"} calls.`;
  } else if (p.ev_bps != null && p.ev_bps < 800) {
    lesson = `Thin-EV entry (${p.ev_bps.toFixed(0)}bps) - higher variance by design. Not necessarily wrong, but worth reviewing if several stack up.`;
  }

  return {
    position: p,
    predictionError,
    overconfident,
    narrative,
    lesson,
  };
}

/** Filter settled positions to only losses. */
export function getLosses(settled: SettledPosition[]): SettledPosition[] {
  return settled.filter(
    (p) => p.settlement_outcome != null && p.settlement_outcome !== p.side,
  );
}
