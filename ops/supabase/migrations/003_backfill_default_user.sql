-- Phase D migration 003 — backfill every legacy row with the default user.
--
-- All rows that existed before multi-tenancy are owned by the synthetic
-- default user. Batched at 10 000 rows per statement for predictable
-- table locks on the larger tables; small tables run in a single shot.

DO $$
DECLARE
    default_uid UUID := '00000000-0000-0000-0000-000000000001';
    affected    BIGINT;
BEGIN
    -- Large, append-heavy tables — batch updates.
    LOOP
        UPDATE pm_positions SET user_id = default_uid
         WHERE user_id IS NULL
         AND id IN (SELECT id FROM pm_positions WHERE user_id IS NULL LIMIT 10000);
        GET DIAGNOSTICS affected = ROW_COUNT;
        EXIT WHEN affected = 0;
    END LOOP;

    LOOP
        UPDATE predictions SET user_id = default_uid
         WHERE user_id IS NULL
         AND id IN (SELECT id FROM predictions WHERE user_id IS NULL LIMIT 10000);
        GET DIAGNOSTICS affected = ROW_COUNT;
        EXIT WHEN affected = 0;
    END LOOP;

    LOOP
        UPDATE market_evaluations SET user_id = default_uid
         WHERE user_id IS NULL
         AND id IN (SELECT id FROM market_evaluations WHERE user_id IS NULL LIMIT 10000);
        GET DIAGNOSTICS affected = ROW_COUNT;
        EXIT WHEN affected = 0;
    END LOOP;

    LOOP
        UPDATE markouts SET user_id = default_uid
         WHERE user_id IS NULL
         AND id IN (SELECT id FROM markouts WHERE user_id IS NULL LIMIT 10000);
        GET DIAGNOSTICS affected = ROW_COUNT;
        EXIT WHEN affected = 0;
    END LOOP;

    -- Smaller tables — one-shot.
    UPDATE performance_snapshots  SET user_id = default_uid WHERE user_id IS NULL;
    UPDATE config_change_history  SET user_id = default_uid WHERE user_id IS NULL;
    UPDATE event_log              SET user_id = default_uid WHERE user_id IS NULL;
    UPDATE news_event_log         SET user_id = default_uid WHERE user_id IS NULL;
    UPDATE user_config            SET user_id = default_uid WHERE user_id IS NULL;
    UPDATE pending_suggestions    SET user_id = default_uid WHERE user_id IS NULL;
END
$$;

-- Sanity check — every per-user table must have zero rows with NULL user_id
-- before migration 004 flips to NOT NULL. Raises if the invariant fails.
DO $$
DECLARE
    nulls BIGINT;
BEGIN
    FOR nulls IN
        SELECT COUNT(*) FROM pm_positions          WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM predictions           WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM market_evaluations    WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM markouts              WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM performance_snapshots WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM config_change_history WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM event_log             WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM news_event_log        WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM user_config           WHERE user_id IS NULL UNION ALL
        SELECT COUNT(*) FROM pending_suggestions   WHERE user_id IS NULL
    LOOP
        IF nulls > 0 THEN
            RAISE EXCEPTION 'Phase D 003: backfill left % rows with NULL user_id', nulls;
        END IF;
    END LOOP;
END
$$;
