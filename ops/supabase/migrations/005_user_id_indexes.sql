-- Phase D migration 005 — indexes on (user_id, created_at DESC).
--
-- Every query that lists rows for a user scopes by user_id and orders by
-- created_at (or evaluated_at / checked_at, as appropriate). A composite
-- index supports both the filter and the sort without a sort step.

CREATE INDEX IF NOT EXISTS idx_pm_positions_user_created
    ON pm_positions (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_predictions_user_created
    ON predictions (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_market_evaluations_user_evaluated
    ON market_evaluations (user_id, evaluated_at DESC);

CREATE INDEX IF NOT EXISTS idx_markouts_user_checked
    ON markouts (user_id, checked_at DESC);

CREATE INDEX IF NOT EXISTS idx_performance_snapshots_user_snapshot
    ON performance_snapshots (user_id, snapshot_date DESC);

CREATE INDEX IF NOT EXISTS idx_config_change_history_user_changed
    ON config_change_history (user_id, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_event_log_user_timestamp
    ON event_log (user_id, "timestamp" DESC);

CREATE INDEX IF NOT EXISTS idx_news_event_log_user_logged
    ON news_event_log (user_id, logged_at DESC);

CREATE INDEX IF NOT EXISTS idx_pending_suggestions_user_created
    ON pending_suggestions (user_id, created_at DESC);
