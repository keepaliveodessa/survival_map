-- =============================================================================
-- quality_report.sql — отчёт качества данных таблицы events
--
-- Использование (на хосте со стеком):
--   docker compose exec -T postgres psql -U postgres -d postgres < postgres/quality_report.sql
-- или по частям: docker compose exec postgres psql -U postgres -d postgres -c "<query>"
--
-- Рекомендуемая периодичность: еженедельно. Блок 4 — корм для geo-справочника
-- (см. подплан N2/N5 улучшения NLP), блоки 1-3 — дашборд-метрики в SQL.
--
-- Контекст TTL: events хранит только последний час — все отчёты ограничены
-- этим окном; для истории используйте срез сразу после чистки.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. Сводка за последний час: объём, доля random, средний confidence
-- -----------------------------------------------------------------------------
SELECT
    COUNT(*)                                                        AS total,
    MIN(event_time)                                                 AS oldest,
    MAX(event_time)                                                 AS newest,
    COUNT(*) FILTER (WHERE strategy = 'random')                     AS random_cnt,
    ROUND(100.0 * COUNT(*) FILTER (WHERE strategy = 'random')
        / NULLIF(COUNT(*), 0), 1)                                   AS random_pct,
    ROUND(AVG(confidence) FILTER (WHERE strategy != 'random')
        ::numeric, 3)                                               AS avg_confidence
FROM events
WHERE event_time >= NOW() - INTERVAL '60 minutes';

-- -----------------------------------------------------------------------------
-- 2. Распределение стратегий × тип геометрии
--    Нарушение пар (например random + LINESTRING) = проблема триггера
--    trg_validate_event_geom — в здоровой системе не встречается.
-- -----------------------------------------------------------------------------
SELECT
    strategy,
    ST_GeometryType(geom)                    AS geom_type,
    COUNT(*)                                 AS cnt,
    ROUND(AVG(confidence)::numeric, 3)       AS avg_confidence
FROM events
GROUP BY strategy, ST_GeometryType(geom)
ORDER BY strategy, cnt DESC;

-- -----------------------------------------------------------------------------
-- 3. Причины random-событий (geo_diagnostics.reason)
--    no_candidates           — матчер не нашёл ни одного кандидата
--    district_only           — район был единственным кандидатом
--    weak_single_candidate   — единственный кандидат ниже порога
--    no_strong_candidates    — гипотезы не построены
--    (NULL)                  — вставка миновала SQL-резолвер (промо/нет гео)
-- -----------------------------------------------------------------------------
SELECT
    COALESCE(geo_diagnostics->>'reason', '(NULL: promo/no-geo path)') AS reason,
    COUNT(*)                                                          AS cnt,
    ROUND(100.0 * COUNT(*)
        / SUM(COUNT(*)) OVER (), 1)                                   AS pct
FROM events
WHERE strategy = 'random'
  AND event_time >= NOW() - INTERVAL '60 minutes'
GROUP BY 1
ORDER BY cnt DESC;

-- -----------------------------------------------------------------------------
-- 4. Кандидаты в geo-справочник: random с «жалобным» текстом
--    Корм для еженедельного пополнения справочника (частотные локации,
--    которые матчер не знает). Смотрите top повторяющихся топонимов.
-- -----------------------------------------------------------------------------
SELECT
    message_id,
    LEFT(description, 120)                                   AS text_preview,
    geo_diagnostics->>'reason'                               AS reason,
    event_time
FROM events
WHERE strategy = 'random'
  AND event_time >= NOW() - INTERVAL '60 minutes'
  AND description NOT IN ('без описания')
ORDER BY event_time DESC
LIMIT 50;

-- -----------------------------------------------------------------------------
-- 5. Zero-геометрия / битый matches (здоровье инвариантов)
--    Ожидается 0. geom NULL легитимен только до триггера валидации —
--    в таблице его быть не должно.
-- -----------------------------------------------------------------------------
SELECT
    COUNT(*) FILTER (WHERE geom IS NULL)                     AS null_geom,
    COUNT(*) FILTER (WHERE matches IS NULL
                       OR jsonb_typeof(matches) <> 'array')  AS bad_matches,
    COUNT(*) FILTER (WHERE strategy = 'random'
                       AND jsonb_array_length(matches) > 0)  AS random_with_matches
FROM events
WHERE event_time >= NOW() - INTERVAL '60 minutes';

-- -----------------------------------------------------------------------------
-- 6. Дубликаты по message_id (редакты канала / аномалии)
--    Технические дубли (одинаковые message_id + event_time) исключены
--    уникальным индексом; разные event_time при одном message_id —
--    редакт сообщения → два события на карте.
-- -----------------------------------------------------------------------------
SELECT
    message_id,
    COUNT(*)       AS event_cnt,
    COUNT(DISTINCT event_time) AS distinct_times
FROM events
GROUP BY message_id
HAVING COUNT(*) > 1
ORDER BY event_cnt DESC
LIMIT 20;

-- -----------------------------------------------------------------------------
-- 7. Health партиций: живые партиции и их свежесть
--    Должно быть 2-3 партиции (TTL 60 минут + окно ±1 час).
--    Рост = чистка clean_old_events не справляется (см. cron.job_run_details).
-- -----------------------------------------------------------------------------
SELECT
    tablename,
    pg_size_pretty(pg_total_relation_size('public.' || tablename)) AS total_size
FROM pg_tables
WHERE tablename LIKE 'events_%'
  AND schemaname = 'public'
ORDER BY tablename DESC
LIMIT 10;

-- -----------------------------------------------------------------------------
-- 8. Прогон pg_cron чистки (пропуски = рост таблицы)
-- -----------------------------------------------------------------------------
SELECT
    jobname,
    status,
    start_time,
    end_time,
    EXTRACT(EPOCH FROM (end_time - start_time)) AS duration_s
FROM cron.job_run_details
WHERE jobname IN ('clean-old-events', 'manage-event-partitions')
ORDER BY start_time DESC
LIMIT 20;
