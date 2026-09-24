-- =============================================================================
-- 17-geo-sync.sql
-- Синхронизация справочника geo с data/geo.csv + пополнение по экспортным
-- промахам (events_export.csv, 2026-09-24).
--
-- ЗАЧЕМ: init-скрипты (04-load-data.sql) выполняются ТОЛЬКО при создании тома.
-- Правки geo.csv на живом развёртывании до сих пор молча не доезжали до БД —
-- матчер в nlp_processor работал по устаревшему справочнику (подтверждено
-- экспортом: «Бугаевска», «Ришельевская» матчатся локально, но не в проде).
--
-- ЭТА МИГРАЦИЯ МНОГОРАЗОВАЯ: любой последующий список объектов для добавления
-- в geo.csv применяется на живом томе этой же миграцией.
--
-- Новые объекты (N2, источник координат):
--   улица Бабеля        — TODO-verify (Overpass был недоступен, приближённо)
--   улица Ждахи         — TODO-verify (приближённо)
--   Сергея Шелухина     — OSM Nominatim (way 29218506 + 1081062242, bbox-центры)
--   Дача Дашкевича      — OSM Nominatim (suburb, точный центр)
--   БКМ                 — TODO-verify (Беляевская КМ, точка у ж/д ветки Беляевки)
--   Великодолинское     — OSM Nominatim (relation 7382285, точный полигон);
--                         народные алиасы Акаржа/Аккаржа/Акарж (live-поток канала)
--   Балтская дорога     — OSM Nominatim (way 52698819, точная линия)
-- Пополнены алиасы существующих: Шестой элемент (елемент/елимент/шкодогорка),
--   Куликово (куликовский/2й куликовский), 9 Фонтана (бык — сквер у 9-й станции,
--   главный ориентир: «Бык блокпост», «возле быка»),
--   Великодолинское (Акаржа/Аккаржа/Акарж) — народные формы из live-потока.
-- Починка типа: Ильичевск числился street при POLYGON-геометрии → town
--   (тип участвует в приоритизации process_candidates_v2 и в матчере).
--
-- ЗАПУСК на живом томе (data/geo.csv запечён в образ при сборке!):
--   docker compose build postgres            # если geo.csv менялся после сборки
--   docker compose up -d postgres            # поднять обновлённый образ
--   docker compose exec -T postgres psql -U postgres -d postgres \
--       -f /docker-entrypoint-initdb.d/17-geo-sync.sql
-- или локально без пересборки:
--   docker compose exec -T postgres psql -U postgres -d postgres < postgres/init-scripts/17-geo-sync.sql
--
-- РЕАКЦИЯ МАТЧЕРА: на новых томах statement-триггер geo_updated (06-*.sql)
-- шлёт pg_notify на каждый INSERT/UPDATE → nlp_processor делает reindex.
-- На старых томах (триггера нет) — явный pg_notify в конце файла.
-- payload без geo_id → полный reindex_all (см. nlp_processor/main.py).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 0. Локальный helper (дублирует 04-load-data.sql: на старых томах его могло
--    не быть; CREATE OR REPLACE идемпотентен). Одна битая WKT не валит миграцию.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION safe_geom_from_text(wkt text, srid int)
RETURNS geometry AS $$
BEGIN
    RETURN ST_SetSRID(ST_GeomFromText(wkt, srid), srid);
EXCEPTION WHEN OTHERS THEN
    RAISE WARNING 'geo-sync: skipping invalid geometry: %', left(wkt, 80);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 1. Загрузить csv во временную таблицу (rn — для дедупа внутри самого csv:
--    две строки, делящие алиас, не должны создать два объекта).
-- -----------------------------------------------------------------------------
CREATE TEMP TABLE temp_geo_sync (
    rn       BIGSERIAL,
    names    TEXT,
    wkt_geom TEXT,
    type     TEXT
);

COPY temp_geo_sync(names, wkt_geom, type)
FROM '/docker-entrypoint-initdb.d/data/geo.csv'
WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');

-- -----------------------------------------------------------------------------
-- 2. INSERT новых объектов.
--    Анти-джойн по ПЕРЕСЕЧЕНИЮ алиасов (&&): объект считается существующим,
--    если в БД есть строка, делящая с csv-строкой хоть один алиас. Это покрывает
--    и полное совпадение каноника, и «csv-алиас = db-каноник».
--    Ряды csv, делящие алиас между собой, вставляются один раз (первый по rn).
-- -----------------------------------------------------------------------------
WITH prep AS (
    SELECT t.rn,
           string_to_array(t.names, '|') AS names_arr,
           t.type,
           safe_geom_from_text(t.wkt_geom, 4326) AS geom
    FROM temp_geo_sync t
    WHERE trim(t.names) <> ''
),
new_only AS (
    SELECT p.names_arr, p.type, p.geom
    FROM prep p
    WHERE p.geom IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM geo g WHERE g.names && p.names_arr)
      AND NOT EXISTS (
          SELECT 1 FROM prep q
          WHERE q.rn < p.rn AND q.names_arr && p.names_arr
      )
)
INSERT INTO geo (names, type, geom)
SELECT names_arr, type, geom FROM new_only;

-- -----------------------------------------------------------------------------
-- 3. Дозаполнение алиасов существующих объектов.
--    Объект сопоставляется по канонику (names[1]); алиасы csv, отсутствующие
--    в БД-строке, дописываются в конец массива. Агрегация по geo_id обязательна:
--    UPDATE ... FROM с несколькими строками-донорами применил бы только одну.
-- -----------------------------------------------------------------------------
WITH prep AS (
    SELECT string_to_array(names, '|') AS names_arr
    FROM temp_geo_sync
    WHERE trim(names) <> ''
),
existing AS (
    SELECT p.names_arr, g.id AS geo_id
    FROM prep p
    JOIN geo g ON g.names[1] = p.names_arr[1]
),
additions AS (
    SELECT e.geo_id, array_agg(DISTINCT a.alias) AS new_aliases
    FROM existing e
    CROSS JOIN LATERAL unnest(e.names_arr) AS a(alias)
    WHERE a.alias <> ''
      AND NOT EXISTS (
          SELECT 1 FROM geo g2
          WHERE g2.id = e.geo_id AND a.alias = ANY(g2.names)
      )
    GROUP BY e.geo_id
)
UPDATE geo g
SET names = array_cat(g.names, ad.new_aliases)
FROM additions ad
WHERE g.id = ad.geo_id;

-- -----------------------------------------------------------------------------
-- 3b. Синхронизация типа: csv — источник истины.
--     Раньше тип существующих объектов НИКОГДА не обновлялся (merge трогал
--     только алиасы): «Ильичевск» дожил бы в БД как street при POLYGON-геометрии.
--     Идемпотентно: на сходящейся БД второй прогон даёт 0 строк.
-- -----------------------------------------------------------------------------
WITH prep AS (
    SELECT string_to_array(names, '|') AS names_arr, type
    FROM temp_geo_sync
    WHERE trim(names) <> ''
),
type_fix AS (
    SELECT g.id AS geo_id, p.type AS new_type
    FROM prep p
    JOIN geo g ON g.names[1] = p.names_arr[1]
    WHERE g.type IS DISTINCT FROM p.type
)
UPDATE geo g
SET type = tf.new_type
FROM type_fix tf
WHERE g.id = tf.geo_id;

-- -----------------------------------------------------------------------------
-- 4. Починка геометрии: строка БД без geom, для которой csv дал валидный WKT.
--    Триггер trg_geo_set_geom_m (15-*.sql) пересчитает geom_m автоматически.
-- -----------------------------------------------------------------------------
WITH prep AS (
    SELECT string_to_array(names, '|') AS names_arr,
           safe_geom_from_text(wkt_geom, 4326) AS geom
    FROM temp_geo_sync
    WHERE trim(names) <> ''
),
heal AS (
    SELECT p.geom, g.id AS geo_id
    FROM prep p
    JOIN geo g ON g.names[1] = p.names_arr[1]
    WHERE g.geom IS NULL AND p.geom IS NOT NULL
)
UPDATE geo g
SET geom = heal.geom
FROM heal
WHERE g.id = heal.geo_id;

-- -----------------------------------------------------------------------------
-- 5. Backfill geom_m для старых томов, где миграция 15 не выполнялась
--    (идемпотентно: только NULL; на актуальных томах — 0 строк).
-- -----------------------------------------------------------------------------
UPDATE geo
SET geom_m = ST_Transform(ST_MakeValid(geom), 3857)
WHERE geom IS NOT NULL AND geom_m IS NULL;

-- -----------------------------------------------------------------------------
-- 6. Отчёт оператору: непокрытые csv-строки (ожидается 0) и наличие 5 новых.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    v_unmatched INT;
    r RECORD;
BEGIN
    SELECT count(*) INTO v_unmatched
    FROM temp_geo_sync t
    WHERE trim(t.names) <> ''
      AND NOT EXISTS (
          SELECT 1 FROM geo g
          WHERE g.names && string_to_array(t.names, '|')
      );
    RAISE NOTICE 'geo-sync: csv rows without DB counterpart: % (expect 0)', v_unmatched;

    FOR r IN
        SELECT t.canon, g.id
        FROM (VALUES ('БКМ'), ('улица Бабеля'), ('улица Ждахи'),
                     ('Сергея Шелухина'), ('Дача Дашкевича'),
                     ('Великодолинское'), ('Балтская дорога')) AS t(canon)
        LEFT JOIN geo g ON g.names[1] = t.canon
    LOOP
        IF r.id IS NULL THEN
            RAISE WARNING 'geo-sync: expected object % is MISSING after sync', r.canon;
        ELSE
            RAISE NOTICE 'geo-sync: % -> id %', r.canon, r.id;
        END IF;
    END LOOP;
END $$;

DROP TABLE temp_geo_sync;

ANALYZE geo;

-- -----------------------------------------------------------------------------
-- 7. Страховка рехайнда для старых томов без триггера geo_updated.
--    payload без geo_id → nlp_processor._on_geo_updated делает полный reindex.
--    На актуальных томах триггер уже отослал нотификации — дубликат безвреден.
-- -----------------------------------------------------------------------------
PERFORM pg_notify('geo_updated', jsonb_build_object(
    'source', '17-geo-sync',
    'message', 'geo dictionary synced from geo.csv'
)::text);

COMMIT;

-- =============================================================================
-- Верификация после запуска (вручную):
--   SELECT id, names, type, ST_AsText(geom) FROM geo
--    WHERE names && ARRAY['бкм','Бабеля','Ждахи','Шелухина','Дашкевича']::text[];
--   SELECT id, names, type FROM geo
--    WHERE names && ARRAY['акаржа','аккаржа','балтская','елемент']::text[];
--   SELECT id, names, type FROM geo WHERE names[1] = 'Ильичевск';  -- type=town
--
-- Ожидаемый эффект на качестве (live_messages.txt, 2026-09-24: 233 не-геолоцированных):
--   «6 елемент/елимент блокпост»              → Шестой элемент (было пусто, ~35 строк)
--   «2й куликовский бус»                      → Куликово (было пусто)
--   «Акаржа/Аккаржа провулок энтузиастов»     → Великодолинское (было пусто)
--   «Балтская дорога ТЦК»                     → Балтская дорога (было пусто)
--   «Бык блокпост», «9 Фонтана возле быка»    → 9 Фонтана (было пусто/одиночка)
--
-- Эффект предыдущего пополнения (см. docs/nlp-подплан N1/N2):
--   «бкм блокпост», «бкм возле Метро»        → БКМ (было random ×3+)
--   «блокпост на Бабеля»                      → улица Бабеля (было random ×2)
--   «Адрес: … улица Ждахи …»                  → улица Ждахи
--   «Адрес: … Дача Дашкевича …»               → Дача Дашкевича
--   «Адрес: 55, Сергея Шелухина улица …»      → Сергея Шелухина (полный адрес)
-- =============================================================================
