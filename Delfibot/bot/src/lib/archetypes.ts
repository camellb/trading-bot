/**
 * Canonical archetype display labels.
 *
 * Source of truth: `Delfibot/bot/local_api.py:ARCHETYPE_META`. The
 * Risk Control page (Risk.tsx) reads its labels live from
 * `/api/archetypes`; the Performance and Intelligence pages don't
 * call that endpoint, so they need a static mirror to keep their
 * copy in sync.
 *
 * Anytime an archetype is added/renamed in the Python catalogue,
 * mirror it here. The fallback `archetypeLabel(id)` humanizes any
 * id not in the map (snake_case -> Title Case) so a missed entry
 * doesn't render as raw `binary_event` to the user.
 */
export const ARCHETYPE_LABELS: Record<string, string> = {
  // Sports
  tennis:              "Tennis",
  basketball:          "Basketball",
  baseball:            "Baseball",
  football:            "Football",
  hockey:              "Hockey",
  cricket:             "Cricket",
  esports:             "Esports",
  soccer:              "Soccer",
  sports_other:        "Other sports",
  // Finance / markets
  crypto:              "Crypto",
  stocks:              "Stocks",
  macro:               "Macro",
  fx_commodities:      "FX & commodities",
  // Politics / society
  election:            "Election",
  policy_event:        "Policy event",
  geopolitical_event:  "Geopolitical event",
  // Tech / culture
  tech_release:        "Tech release",
  awards:              "Awards",
  entertainment:       "Entertainment",
  // Catch-alls
  weather_event:       "Weather event",
  price_threshold:     "Price threshold (other)",
  activity_count:      "Activity count",
  binary_event:        "Other events",
};

/** Resolve an archetype id to its display label. Falls back to
 *  humanized snake_case when the id isn't in the canonical map. */
export function archetypeLabel(id: string | null | undefined): string {
  if (!id) return "Unknown";
  if (ARCHETYPE_LABELS[id]) return ARCHETYPE_LABELS[id];
  return id
    .split("_")
    .map((w) => (w.length === 0 ? w : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}
