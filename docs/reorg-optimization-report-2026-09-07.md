# Отчёт: реорганизация и оптимизация кодовой базы — Survival Map

**Дата:** 2026-09-07
**Объём:** полный проход по коду всех модулей (processor, core, parser, common, web, postgres, tests)
**Базлайн верификации:** `pytest tests/` → **325 passed, 26 skipped** (чистый прогон на текущем дереве)
**Границы работ:** реорганизация/оптимизация **внутри отдельных модулей**. Общая архитектура
(5 микросервисов, поток данных `parser → pending_events → processor → events → pg_notify → core → ws → web`,
слои Docker-сети, разделение ответственности) — **не трогается**. Отчёт не предлагает ни одного
межсервисного изменения.

---

## 0. Резюме

Кодовая база в хорошем состоянии: недавние рефакторинги (NLP-geo 2026-09-06, hardening 2026-08-28)
закрыли большинство прошлых находок. Оставшиеся возможности — это **мёртвый код внутри модулей**,
**дубликаты в пределах одного модуля**, **мелкие оптимизации горячих путей** и **инконсистентности
внутри файлов**. Ничего из найденного не требует изменения архитектуры или контрактов между
сервисами.

| Категория | Кол-во | Характер |
|---|---|---|
| 🔴 Мёртвый код / неиспользуемое | 8 | Целые классы/методы без единого вызывающего |
| 🟠 Дубли внутри модуля | 5 | Один и тот же код дважды в одном файле/паре файлов |
| 🟡 Оптимизация горячих путей | 6 | Аллокации, повторные вызовы, O(n)-сканы |
| 🟢 Мелкая гигиена | 7 | Логи, нейминг, мелкие фиксы |

Общий эффект: −700…−900 строк мёртвого/дублированного кода, устранение единственного
известного «горячего» тормоза (Tier-2 fuzzy), ноль изменений поведения.

---

## 1. 🔴 Мёртвый код (удалить)

### 1.1. `core/db/db_spatial.py` — весь модуль мёртв (206 строк)

Класс `SpatialOperations` (методы `get_geo_intersection`, `get_geo_nearby_intersection`,
`get_batch_intersections`, `get_max_distance_in_polygon`) **не имеет ни одного вызывающего
в production-коде**: grep по `core/ processor/ parser/ common/ scripts/` находит обращения
только из фасада `dbconnect.py` (простые делегаты) и из тестов `test_db_spatial.py`.
Геометрический арбитраж полностью переехал в SQL-функцию `process_candidates_v2`
(вызывается из processor), Python-версии больше не нужны.

**Действие:** удалить `core/db/db_spatial.py`, 4 метода-делегата из `dbconnect.py`
и `tests/test_db_spatial.py`. Фасад `Request` похудеет на 4 метода без изменения
поведения остальных вызовов.

### 1.2. `core/db/db_events.py` — мёртвые методы

- `delete_old_events()` — TTL чистится pg_cron-партициями (init-scripts/11) и
  `clean_old_events()` (03-functions.sql, порог 60 мин — расхождение из старого
  ревью уже исправлено). Python-версия никем не вызывается → удалить метод
  + делегат в `dbconnect.py` + тест.
- `get_latest_update_time()` — вызывается только через **три разных алиаса**
  фасада (`get_latest_events_update_time`, `get_latest_event_time`,
  `get_latest_update_time` в `db_geo`). Из них реально используется один
  (`get_latest_event_time` из `core/api/events.py::get_data_status_handler`).
  Остальные два делегата фасада — мёртвые → удалить.
- `get_incremental_events()` — живой (используется `get_events_handler` при `since`), **не трогать**.

### 1.3. `core/utils/cache.py` — `_make_key()` (L2 из старого ревью, до сих пор жив)

Метод не вызывается нигде в production; единственные обращения — его же тесты
(`test_make_key_with_args/kwargs`). **Действие:** удалить метод и оба теста.

### 1.4. `processor/health.py` — `record_message_processed()` / `record_error()`

Пустые no-op методы с TODO «integrate prometheus_client». Проект **осознанно удалил**
стек мониторинга (см. codebase-review §5 «Мониторинг-стек удалён»), и в `processor/main.py`
оба метода всё ещё вызываются (2 вызова). **Действие:** удалить методы и оба вызова
в `main.py` (строки `self.health_server.record_message_processed(...)` × 2,
`record_error()` × 1) — счётчики `_messages_processed`/`_errors` уже ведутся в самом боте
и попадают в heartbeat/логи.

### 1.5. `core/api/health.py` — `_check_db_cached()` shim

Backwards-compat обёртка без единого вызывающего (`health_live_handler` её не использует).
**Действие:** удалить функцию.

### 1.6. `common/settings.py` — неиспользуемые поля `SimilarityConfig`

Пять полей не читаются нигде кроме объявления (grep по всей кодовой базе пуст):
`entity_similarity_threshold`, `phonetic_match_threshold` (только в info-логе старта),
`lemma_fallback_enabled`, `pseudo_intersection_radius_meters`, `midpoint_max_distance_m`,
`midpoint_types`, `geometry_min_score`. Это рудименты старого Python-резолвера
(`get_geo_nearby_intersection` и «tier-3 lemma fuzzy»), которых больше нет.
**Действие:** удалить 7 полей (оставить только то, что читает `geo_matcher.py`:
`surface_typo_threshold`, `max_sliding_window`, `prepositional_boost`, `max_entities`,
`enable_pos_filter`, `punctuation_tokens`). Заодно исчезает вводящий в заблуждение
info-лог «Geo matcher settings: phonetic_threshold=…, lemma_threshold=…» в `processor/main.py::_init_nlp`.

### 1.7. `processor/morphology.py` — `lemma_for_phrase()` + `_phrase_cache`

Метод и его LRU-кэш (2000 записей) не имеют вызывающих: докстринг ссылается на
«_build_alias_index», которого больше нет (индекс строится через `stem_tokens` в
`PhoneticIndex._stem_tuple_for_name`). **Действие:** удалить метод, кэш и ветку в `shrink_cache()`
(оставить только lemma/stem кэши).

### 1.8. `core/db/db_adapter.py` — дубликат `common/db_adapter.py`

Файлы идентичны с точностью до одной строки импорта (`core.db.db_base` vs `common.db.base`).
Все реальные импорты идут через `common.db_adapter` (parser, processor, tests); `core/db/db_adapter.py`
никто не импортирует. **Действие:** удалить файл из `core/db/`.

---

## 2. 🟠 Дубликаты внутри модулей (схлопнуть)

### 2.1. `processor/geo_matcher.py` — Tier-2 логика продублирована

Логика «fuzzy-матч + prefix-guard + length-guard + short-settlement guard» существует
в двух местах: `_link_span()` (для внешних вызовов/тестов) и батч-блок в `find_geo()`.
Два блока по ~40 строк отличаются только способом вызова executor'а. Риск: guard'ы
разъедутся при следующей правке (уже есть расхождение — в `_link_span` fallback
использует `fuzz.ratio`, в `find_geo` — `fuzz.WRatio`).

**Действие:** извлечь общий приватный хелпер
`_accept_typo_match(surface, match, score, idx) -> Optional[Dict]` и использовать в обоих
местах. Поведение не меняется, диф ~50 строк.

### 2.2. `processor/main.py` — два почти одинаковых INSERT-SQL

`_INSERT_EVENT_SIMPLE` и `_INSERT_EVENT_FROM_CANDIDATES` делят идентичные блоки
`meta_upd` и `notify_call` (pg_notify payload). Дублирование ~20 строк SQL, которое
уже разъезжалось в истории (комментарий R-DB0 продублирован в обоих).

**Действие:** собрать SQL из общего шаблона (два f-string с общей «хвостовой» частью),
или как минимум вынести `notify_call`-блок в одну константу-фрагмент.

### 2.3. `core/api/websocket.py` — `orjson.dumps(...).decode()` × 10

Паттерн `orjson.dumps({...}).decode()` повторяется 10 раз; в `_broadcast_payload` ещё и
условие `payload.decode() if isinstance(payload, bytes) else payload` — гибридный
контракт «то bytes, то str». **Действие:** ввести локальный хелпер `_json_msg(type_, **fields) -> str`
и привести контракт `_broadcast_payload` к одному типу (str). Меньше шансов забыть `.decode()`.

### 2.4. `web/js/core/ui.ts` — `hideMaplibreLabels()` vs `_applyDarkTheme()`

Обе функции начинают с идентичного цикла «скрыть все symbol-слои» (копипаста ~12 строк).
**Действие:** извлечь `_hideSymbolLayers(glMap)` и вызвать из обоих.

### 2.5. `web/js/core/ui.ts` — трижды повторённый паттерн «maplibreGL + theme»

Конструкция `(L as unknown as {maplibreGL...}).maplibreGL({style})` + проверка
`isStyleLoaded/once('load')` повторяется трижды (switchTileLayer, initializeMap local,
initializeMap maplibre-ветка). **Действие:** хелпер `_addMaplibreLayer(map, style, theme?)`.

---

## 3. 🟡 Оптимизация горячих путей

### 3.1. `processor/geo_matcher.py` — Tier-2: перенос cdist-ускорителя в прод (главный пункт)

Уже доказано офлайн-исследованием (quality-report-live-messages-2026-09-06 §6):
`process.extractOne(WRatio)` по ~4k алиасов на каждое мусорное окно даёт **~7 с/сообщение**;
префильтр по токен-префиксам + `rapidfuzz.process.cdist` даёт **74 мс** при parity 28/28.
Харнесс с parity-контролем уже существует (`scripts/eval_live_parallel.py`).

**Действие:** перенести префильтр+cdist в `_batch_fuzzy_match` (или рядом), сохранив
fallback на `extractOne` при расхождении (fail-closed, как в харнессе). Это единственная
оптимизация с измеримым 100×-эффектом; всё остальное в отчёте — гигиена.

### 3.2. `processor/main.py` — `run()`-цикл: `check_memory()` читает `/proc` каждую секунду

`get_rss_mb()` открывает и парсит `/proc/self/status` каждый цикл (1 с) + ещё раз в
`_write_heartbeat` в том же цикле. **Действие:** читать RSS один раз за итерацию и
передавать в оба потребителя (или кэшировать на 5 с в `HealthServer`). Мелочь, но
это самый частый цикл в сервисе.

### 3.3. `processor/main.py` — `_worker_tasks`-фильтрация дважды за цикл

`[t for t in self._worker_tasks if not t.done()]` выполняется и в `run()` (каждую секунду),
и в каждом `_spawn_worker()`. Список из 5–8 элементов — дёшево, но фильтрацию в `run()`
можно убрать совсем: `_supervise_worker` уже перезапускает упавших, а список чистится
при spawn. **Действие:** оставить фильтрацию только в `_spawn_worker`.

### 3.4. `core/api/events.py` — `get_events_handler`: лишний `get_events_meta()` при cache-hit

При попадании в кэш handler всё равно делает `await db_request.get_events_meta()` —
отдельный SQL-запрос на каждый REST-запрос ради ETag. **Действие:** кэшировать
`(version, etag)`-пару рядом с `_etag_cache` (обновлять при промахе кэша GeoJSON),
чтобы при HIT не ходить в БД вовсе. Экономит один round-trip на каждый запрос списка.

### 3.5. `core/api/websocket.py` — `send_events_since`: поэлементная отправка features

Цикл `for feature in features: await ws.send_str(...)` — по одному WS-кадру и одному
`orjson.dumps` на фичу (до 5000 при snapshot). **Действие:** батчировать (например,
чанки по 100 фич в одном `feature_batch`-сообщении) — в 50–100× меньше кадров и
сериализаций. Требует зеркальной правки в `web/js/core/websocket.ts` (`handleMessage`):
принимать `feature_batch.data: Feature[]`. Оба конца — в одном модуле, контракт
остальных типов (`feature`, `events_snapshot_end`) не меняется.

### 3.6. `web/js/core/store.ts` — `pruneExpired`: полная сортировка при hard cap

При переполнении >5000 сортируется весь Map O(n log n). Событие редкое, но дешёвая
замена есть: `partial`-выборка через `Array.prototype.sort` по уже собранному массиву
всё равно O(n log n); альтернативнее — поддерживать min-heap по времени или просто
выбрать N самых старых одним проходом (selection по `time`, O(n·overflow)). Учитывая
редкость события — **низкий приоритет**, отметить как «по желанию».

---

## 4. 🟢 Гигиена и мелкие фиксы

| # | Файл | Что |
|---|---|---|
| G1 | `core/models.py` | `validate_layers_list` мутирует входной список `v[i] = layer.strip()` — для Pydantic v2 корректнее вернуть новый список; заодно дублирует валидацию из `core/utils/validators.py::validate_layers` (два источника истины для одного правила). Оставить один: Pydantic-валидатор делегирует `validate_layers()` |
| G2 | `core/handlers/basic.py` | `logger.critical` на ошибке отправки приветствия — это `error`, не critical (L5 из старого ревью) |
| G3 | `core/utils/cache.py` | camelCase `getItem/setItem` — переименовать в `get/set` (внутренний API, 6 точек вызова внутри файла) |
| G4 | `core/api/health.py` | `health_detailed_handler` возвращает `uptime: request.app.get('start_time', 0)` — это epoch-метка, а не uptime. Заменить на `time.time() - start_time` |
| G5 | `core/api/events.py` | `get_events_status_handler` читает и выбрасывает тело POST (`await request.json()` в try/pass) — либо документировать как «body ignored», либо убрать чтение |
| G6 | `parser/monitoring.py` | `_recover_missing_photos()` выполняется последовательно в цикле старта — при большом числе пропущенных фото тормозит старт; обернуть в `asyncio.gather` с семафором (семафор `_download_semaphore` уже есть) |
| G7 | `processor/geo_matcher.py` | `_link_span` fallback-ветка использует `fuzz.ratio`, батч-путь — `fuzz.WRatio` (см. 2.1) — унифицировать при схлопывании дубля |

---

## 5. Верифицированные «не-проблемы» (проверено, исправлено ранее)

Эти пункты из старых ревью **уже исправлены** — встречаются в старых отчётах, но в коде
их больше нет (проверено grep'ом):

- `asyncio.get_event_loop()` — 0 вхождений в Python-коде;
- `self._db.pool.acquire()` bypass в `db_events.py` — 0 вхождений;
- bot-token prefix на INFO — теперь `logger.debug` (`app_factory.py`);
- trailing comma в cops keywords — исправлено;
- `session.session` в git — не отслеживается;
- `process_candidates_v2` street_segment/geometrytype-guard — присутствует (строки 116/126 SQL);
- TTL-расхождение 48h vs 60min — исправлено (03-functions: 60 минут);
- `cleanup_expired_refresh_tokens()` без cron — **cron.schedule присутствует** (17-refresh-tokens.sql:42);
- мёртвые stale `.js` файлы фронтенда — удалены, остались только `.ts`.

---

## 6. Явно НЕ трогаем (осознанные архитектурные решения)

1. **5 микросервисов и их границы** — parser/processor/core/postgres/web остаются как есть.
2. **`window.*`-глобалы фронтенда (93 штуки)** — известная техническая задолженность (L9),
   но унификация требует изменения системы загрузки скриптов (map-bootstrap → webpack),
   что выходит за рамки «внутримодульной» реорганизации.
3. **`tsconfig.json strict: false`** — включение strict mode — отдельный проект с
   гарантированной волной правок типов; не гигиена.
4. **`StorageAdapter` async-обёртка над синхронным localStorage** — интерфейс осознанно
   оставлен async (`AsyncStorage` из types), чтобы не менять API `LocalCache`.
5. **pg_notify как единственный брокер** — ограничения горизонтального масштабирования
   задокументированы в production-readiness-review §1.6 и принимаются.
6. **`processor/health.py` без cleanup runner'а** — сервер живёт до конца процесса,
   cleanup не нужен (в отличие от core, где health встроен в main-app).

---

## 7. Приоритизированный план

### Пакет 1 — «Мёртвый код» (безопасно, ~1 час, −600…−800 строк)
1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.7 → 1.8 → 1.6 (последним — он widest).

После каждого шага: `pytest tests/ -q` (325 passed должен сохраниться; несколько тестов
удаляются вместе с кодом — их количество в baseline пересчитать).

### Пакет 2 — «Дубли» (~2 часа, нулевой риск поведения)
2.1 → 2.2 → 2.3 → 2.4+2.5.

### Пакет 3 — «Горячие пути» (~3–4 часа, с замерами)
3.1 (главный, с parity-тестом на live-экспорте) → 3.4 → 3.5 (пара backend+frontend)
→ 3.2 → 3.3 → 3.6 (опционально).

### Пакет 4 — «Гигиена» (~1 час)
G1…G7 в любом порядке.

**Критерий приёмки каждого пакета:** `pytest tests/` зелёный; `npx tsc --noEmit` +
`npx eslint js --ext .ts,.js` в `web/` (для пакетов 2–3); для 3.1 — повторный прогон
`scripts/eval_live_parallel.py` с parity-контролем (28/28) и замером мс/сообщение.

---

## 8. Сводная таблица изменений по файлам

| Файл | Действие | Строк |
|---|---|---|
| `core/db/db_spatial.py` | удалить целиком | −206 |
| `core/db/dbconnect.py` | удалить 4 spatial-делегата + 2 алиаса `get_latest_*` + `delete_old_events` | −25 |
| `tests/test_db_spatial.py` | удалить | −116 |
| `core/db/db_events.py` | удалить `delete_old_events` | −20 |
| `core/db/db_adapter.py` | удалить (дубликат common/) | −150 |
| `core/utils/cache.py` | удалить `_make_key`, rename getItem/setItem | −10 |
| `core/api/health.py` | удалить `_check_db_cached`, фикс uptime | −8 |
| `processor/health.py` | удалить no-op record_* | −12 |
| `processor/main.py` | убрать вызовы record_*, дедуп SQL, RSS-кэш | −30 |
| `processor/morphology.py` | удалить `lemma_for_phrase` + phrase-кэш | −30 |
| `common/settings.py` | удалить 7 неиспользуемых полей SimilarityConfig | −25 |
| `processor/geo_matcher.py` | схлопнуть Tier-2 дубль, cdist-ускоритель | ±80 |
| `core/api/websocket.py` | хелпер `_json_msg`, батч-отправка | ±40 |
| `web/js/core/websocket.ts` | приём `feature_batch` | +15 |
| `web/js/core/ui.ts` | 2 хелпера вместо копипасты | −40 |
| `core/api/events.py` | etag-кэш без лишнего SQL | −5 |

Итого: **−650…−900 строк**, ноль изменений контрактов между сервисами, ноль изменений схемы БД.
