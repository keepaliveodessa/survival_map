-- 02-tables.sql
-- Минимальная схема без избыточности
-- Events partitioned by day for high-churn performance

-- Справочник geo-объектов: улицы, нас.пункты, POI
CREATE TABLE IF NOT EXISTS geo (
    id SERIAL PRIMARY KEY,
    names TEXT[] NOT NULL,
    type TEXT NOT NULL DEFAULT 'street',
    geom GEOMETRY(Geometry, 4326) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geo_names ON geo USING gin (names);
CREATE INDEX IF NOT EXISTS idx_geo_geom ON geo USING gist (geom);

-- Стоп-слова
CREATE TABLE IF NOT EXISTS stopwords (word TEXT PRIMARY KEY);

-- Ключевые слова для определения слоя
CREATE TABLE IF NOT EXISTS layer_keywords (
    layer TEXT PRIMARY KEY,
    keywords TEXT[] NOT NULL
);

-- Основная таблица событий (партиционирована по часам)
-- Realtime only: TTL 60 минут, максимум 2-3 партиции.
-- Инварианты:
--   layer — закрытое множество слоёв;
--   description — ограничение 500 символов.
CREATE TABLE IF NOT EXISTS events (
    id SERIAL,
    message_id BIGINT,
    event_time TIMESTAMPTZ NOT NULL,
    description TEXT NOT NULL CHECK (char_length(description) <= 500),
    photo_url TEXT,
    layer TEXT NOT NULL DEFAULT 'pig'
        CHECK (layer IN ('pig', 'cops', 'bus', 'traffic')),
    matches JSONB,
    -- Множество стратегий = фактический выход process_candidates_v2
    -- (16-process-candidates-v2.sql). random_null в events не попадает:
    -- nlp_processor конвертирует его в random (R-PR22).
    strategy VARCHAR(40) NOT NULL CHECK (strategy IN (
        'random',
        'single_match',
        'intersection',
        'street_segment',
        'weighted_centroid'
    )),
    geom GEOMETRY,
    confidence FLOAT DEFAULT 0.0,
    geo_diagnostics JSONB,
    PRIMARY KEY (id, event_time)
) PARTITION BY RANGE (event_time);

-- Примечание: миграционный блок ниже (DROP/UPDATE/ADD CONSTRAINT) нужен только
-- для томов, созданных со старой схемой v1; на fresh-томе он идемпотентен.
-- Создаём партиции с -1 часа до +1 часа вперёд
DO $$
DECLARE
    part_hour TIMESTAMPTZ;
    part_name TEXT;
BEGIN
    FOR i IN -1..1 LOOP
        part_hour := date_trunc('hour', NOW()) + i * INTERVAL '1 hour';
        part_name := 'events_' || to_char(part_hour, 'YYYY_MM_DD_HH24');
        IF NOT EXISTS (
            SELECT 1 FROM pg_class WHERE relname = part_name
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF events FOR VALUES FROM (%L) TO (%L)',
                part_name,
                part_hour,
                part_hour + INTERVAL '1 hour'
            );
        END IF;
    END LOOP;
END;
$$;

-- Индексы на партиционированной таблице (создаются на родителе и наследуются)
CREATE INDEX IF NOT EXISTS idx_events_geom ON events USING gist (geom);
CREATE INDEX IF NOT EXISTS idx_events_layer ON events(layer);
CREATE INDEX IF NOT EXISTS idx_events_message_id ON events(message_id);

-- message_id + event_time = уникальны (partition key обязан входить в unique index).
-- NULL в unique-индексе не равен NULL, поэтому WHERE не нужен.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_message_id_unique ON events(message_id, event_time);

-- CHECK strategy: синхронизация живого тома с актуальным множеством.
-- Исторические значения v1 (midpoint/proximity/cluster_centroid) мигрируются в
-- weighted_centroid, затем CHECK пересоздаётся в суженном виде.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_strategy_check;
UPDATE events SET strategy = 'weighted_centroid'
WHERE strategy IN ('midpoint', 'proximity', 'cluster_centroid');
ALTER TABLE events ADD CONSTRAINT events_strategy_check
    CHECK (strategy IN (
        'random',
        'single_match',
        'intersection',
        'street_segment',
        'weighted_centroid'
    ));

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_layer_check;
ALTER TABLE events ADD CONSTRAINT events_layer_check
    CHECK (layer IN ('pig', 'cops', 'bus', 'traffic'));

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_description_length;
ALTER TABLE events ADD CONSTRAINT events_description_length
    CHECK (char_length(description) <= 500);

-- Метаданные для синхронизации WebSocket
CREATE TABLE IF NOT EXISTS events_meta (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version INT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now(),
    max_event_id INT DEFAULT 0
);

INSERT INTO events_meta (id, version, updated_at, max_event_id)
VALUES (1, 0, now(), 0)
ON CONFLICT (id) DO NOTHING;
