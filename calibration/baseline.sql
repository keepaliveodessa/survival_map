-- Baseline queries for NLP quality calibration
-- Run against the events table to understand current performance

-- 1. Strategy distribution (how geo-matching resolves candidates)
SELECT
    strategy,
    COUNT(*) as event_count,
    ROUND(AVG(confidence), 3) as avg_confidence,
    ROUND(MIN(confidence), 3) as min_confidence,
    ROUND(MAX(confidence), 3) as max_confidence
FROM events
WHERE strategy IS NOT NULL
GROUP BY strategy
ORDER BY event_count DESC;

-- 2. Layer distribution (NLP classification)
SELECT
    layer,
    COUNT(*) as event_count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM events), 1) as percentage
FROM events
WHERE layer IS NOT NULL
GROUP BY layer
ORDER BY event_count DESC;

-- 3. Confidence score distribution (buckets for analysis)
SELECT
    CASE
        WHEN confidence < 0.5 THEN 'low (<0.5)'
        WHEN confidence < 0.7 THEN 'medium (0.5-0.7)'
        WHEN confidence < 0.85 THEN 'high (0.7-0.85)'
        ELSE 'very_high (0.85+)'
    END as confidence_bucket,
    COUNT(*) as event_count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM events), 1) as percentage
FROM events
WHERE confidence IS NOT NULL
GROUP BY confidence_bucket
ORDER BY min(confidence);

-- 4. Geo candidate count distribution
SELECT
    jsonb_array_length(matches) as candidate_count,
    COUNT(*) as event_count
FROM events
WHERE matches IS NOT NULL
GROUP BY candidate_count
ORDER BY candidate_count;

-- 5. Daily processing volume (for trend analysis)
SELECT
    DATE(event_time) as day,
    COUNT(*) as total_events,
    SUM(CASE WHEN strategy = 'random' THEN 1 ELSE 0 END) as random_events,
    SUM(CASE WHEN strategy = 'random' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as random_pct
FROM events
WHERE event_time >= NOW() - INTERVAL '7 days'
GROUP BY day
ORDER BY day;

-- 6. Strategy × Layer cross-tabulation (identify weak combinations)
SELECT
    strategy,
    layer,
    COUNT(*) as event_count,
    ROUND(AVG(confidence), 3) as avg_confidence
FROM events
WHERE strategy IS NOT NULL AND layer IS NOT NULL
GROUP BY strategy, layer
ORDER BY strategy, event_count DESC;

-- 7. Low-confidence events (potential calibration targets)
SELECT
    id,
    LEFT(description, 80) as text_preview,
    strategy,
    confidence,
    layer,
    event_time
FROM events
WHERE confidence IS NOT NULL
  AND confidence < 0.7
  AND strategy != 'random'
ORDER BY confidence ASC
LIMIT 100;

-- 8. Random-point events (no geo match — analyze patterns)
SELECT
    COUNT(*) as total_random,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM events), 1) as pct_of_total
FROM events
WHERE strategy = 'random';

-- 9. Anti-list triggers (single_match downgraded due to distance)
-- Query geo_diagnostics for anti_list_reason
SELECT
    COUNT(*) as anti_list_count,
    ROUND(COUNT(*) * 100.0 /
        (SELECT COUNT(*) FROM events WHERE strategy IS NOT NULL), 1) as pct
FROM events
WHERE geo_diagnostics ? 'anti_list_reason';

-- 10. Average matches per layer (measure geo coverage)
SELECT
    layer,
    ROUND(AVG(CASE WHEN jsonb_array_length(matches) > 0 THEN 1 ELSE 0 END), 3) as geo_match_rate,
    ROUND(AVG(confidence), 3) as avg_confidence
FROM events
WHERE layer IS NOT NULL
GROUP BY layer;
