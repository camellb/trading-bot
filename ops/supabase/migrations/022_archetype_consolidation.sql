-- Migration 022 - collapse legacy sub-tier archetype labels to the flat
-- one-label-per-sport taxonomy.
--
-- Background
-- ----------
-- The classifier (`apps/bot/engine/archetype_classifier.py`) was
-- simplified to a flat, sport-level taxonomy: one label per sport
-- (tennis, basketball, baseball, football, hockey, cricket, esports,
-- soccer) plus a sports_other catch-all and four non-sports labels
-- (price_threshold, activity_count, geopolitical_event, binary_event).
-- Earlier classifier versions split these into sub-tiers
-- ("tennis_qualifier", "basketball_prop", "sports_match",
-- "geopolitical", etc.). Historical rows in pm_positions,
-- market_evaluations, and predictions still carry those legacy labels.
--
-- Leaving legacy labels in place causes the dashboard's "Market
-- categories" panel to surface chips that do not map to what the
-- classifier actually emits, and causes per-category analytics to
-- double-bucket the same sport (e.g., "tennis" and "tennis_qualifier"
-- both present).
--
-- This migration rewrites legacy labels in every archetype column.
-- Idempotent: rows that already hold a canonical label are untouched
-- and a second run is a no-op because no row matches a legacy key
-- after the first run.

BEGIN;

-- Deterministic label mapping. Expand this block if future classifier
-- simplifications retire more labels.
WITH legacy_map(legacy, canonical) AS (
    VALUES
        ('tennis_qualifier',     'tennis'),
        ('tennis_main_draw',     'tennis'),
        ('tennis_lower_tier',    'tennis'),
        ('basketball_prop',      'basketball'),
        ('basketball_game',      'basketball'),
        ('baseball_game',        'baseball'),
        ('baseball_prop',        'baseball'),
        ('football_game',        'football'),
        ('football_prop',        'football'),
        ('hockey_game',          'hockey'),
        ('hockey_prop',          'hockey'),
        ('esports_match',        'esports'),
        ('soccer_match',         'soccer'),
        ('cricket_match',        'cricket'),
        ('sports_match',         'sports_other'),
        ('sports_prop',          'sports_other'),
        ('geopolitical',         'geopolitical_event')
)
UPDATE pm_positions p
   SET market_archetype = m.canonical
  FROM legacy_map m
 WHERE p.market_archetype = m.legacy;

WITH legacy_map(legacy, canonical) AS (
    VALUES
        ('tennis_qualifier',     'tennis'),
        ('tennis_main_draw',     'tennis'),
        ('tennis_lower_tier',    'tennis'),
        ('basketball_prop',      'basketball'),
        ('basketball_game',      'basketball'),
        ('baseball_game',        'baseball'),
        ('baseball_prop',        'baseball'),
        ('football_game',        'football'),
        ('football_prop',        'football'),
        ('hockey_game',          'hockey'),
        ('hockey_prop',          'hockey'),
        ('esports_match',        'esports'),
        ('soccer_match',         'soccer'),
        ('cricket_match',        'cricket'),
        ('sports_match',         'sports_other'),
        ('sports_prop',          'sports_other'),
        ('geopolitical',         'geopolitical_event')
)
UPDATE market_evaluations e
   SET market_archetype = m.canonical
  FROM legacy_map m
 WHERE e.market_archetype = m.legacy;

-- NOTE: `predictions` does not carry `market_archetype` (only `pm_positions`
-- and `market_evaluations` do, per apps/bot/db/models.py lines 454-457).
-- An earlier draft of this migration touched `predictions`; that was
-- wrong and would ERROR out. Only the two columns above exist in prod.

COMMIT;
