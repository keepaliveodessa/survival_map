-- =============================================================================
-- 21-autovacuum-tuning.sql
-- Table-specific autovacuum settings for high-churn tables.
--
-- These override global autovacuum_* settings from postgresql.conf.
-- Events table: 60-min TTL, high INSERT rate, hourly partitions.
-- Pending events: queue with status transitions, moderate churn.
-- =============================================================================

-- =============================================================================
-- Events table — SKIPPED
-- =============================================================================
-- events is a PARTITION BY RANGE table (R-DB2). ALTER TABLE ... SET (...)
-- with storage parameters on a partitioned parent can fail on some PG versions.
-- Global autovacuum settings in postgresql.conf (R-DB18) already cover all
-- partitions: autovacuum_vacuum_scale_factor=0.05, naptime=20s, etc.
--
-- NOTE: autovacuum_truncate_scale_factor does NOT exist in PostgreSQL;
-- autovacuum truncation is controlled by autovacuum_truncate (boolean).

-- =============================================================================
-- Pending events table — queue with status transitions
-- =============================================================================
-- Status changes: pending → processing → done/error
-- Rows are rarely updated twice, but 'done' rows accumulate until cleanup.
-- Moderate INSERT rate from parser, moderate DELETE rate from cleanup.

ALTER TABLE pending_events SET (
    -- Vacuum after 5% changed (same as global, but explicit)
    autovacuum_vacuum_scale_factor = 0.05,

    -- Analyze after 2% changed
    autovacuum_analyze_scale_factor = 0.02,

    -- Lower threshold for queue table
    autovacuum_vacuum_threshold = 300,

    autovacuum_analyze_threshold = 150,

    -- Moderate cost limit
    autovacuum_vacuum_cost_limit = 1500,

    -- 2ms delay (same as global)
    autovacuum_vacuum_cost_delay = 2
);

-- =============================================================================
-- Geo table — low churn, read-heavy
-- =============================================================================
-- Geo objects are rarely updated (manual admin changes).
-- Mostly read by nlp_processor for matching. No aggressive vacuum needed.

ALTER TABLE geo SET (
    -- Higher thresholds: geo table is stable
    autovacuum_vacuum_scale_factor = 0.1,
    autovacuum_analyze_scale_factor = 0.05,
    autovacuum_vacuum_threshold = 1000,
    autovacuum_analyze_threshold = 500,

    -- Lower priority: don't compete with events vacuum
    autovacuum_vacuum_cost_limit = 500,
    autovacuum_vacuum_cost_delay = 5
);

-- =============================================================================
-- Log autovacuum settings for verification
-- =============================================================================
-- In PG15, per-table autovacuum settings live in pg_class.reloptions
-- (text array), not as direct columns. Extract them with regexp.
DO $$
DECLARE
    r RECORD;
    opts TEXT[];
    val TEXT;
BEGIN
    FOR r IN
        SELECT n.nspname AS schemaname,
               c.relname,
               c.reloptions
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.reloptions IS NOT NULL
          AND array_length(c.reloptions, 1) > 0
        ORDER BY c.relname
    LOOP
        RAISE NOTICE 'Table %.%: reloptions=%',
            r.schemaname, r.relname, r.reloptions;
    END LOOP;
END;
$$;
