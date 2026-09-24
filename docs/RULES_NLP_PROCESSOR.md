# Rules — Processor Service (NLP Pipeline) v2.1

**Сервис:** `nlp_processor/` (pymorphy3 + rapidfuzz + PostGIS)
**Точка входа:** `python -m nlp_processor.main`
**Порт:** Нет (только heartbeat healthcheck)
**Docker:** Multi-stage build, `COPY --chown=nlp_processor:nlp_processor`, non-root, tmpfs /tmp 50m

---

## 1. Архитектурные правила

### R-PR1: Processor — ЕДИНСТВЕННЫЙ NLP обработчик

Processor — единственный сервис, выполняющий NLP-пайплайн:
```
pending_events → tokenize → lemmatize → classify → find_geo → process_candidates → INSERT INTO events
```

**Запрещено:**
- NLP-код в parser (кроме `text_preprocessor.py`)
- NLP-код в core
- Дублирование логики лемматизации/классификации в других сервисах

### R-PR2: Async архитектура — worker pool

Processor работает в одном asyncio event loop с пулом воркеров:

```python
self._worker_concurrency = max(1, min(8, settings.nlp_processor.worker_concurrency))

for _ in range(self._worker_concurrency):
    self._spawn_worker()
```

**Правило:** Каждый воркер потребляет одну запись из `pending_events` через `FOR UPDATE SKIP LOCKED`.

### R-PR3: Graceful shutdown — drain + cancel

При SIGTERM nlp_processor:
1. `self._running = False` — воркеров перестают появляться новые
2. Все worker tasks отменяются через `task.cancel()`
3. `asyncio.gather(*tasks, return_exceptions=True)` — ждём завершения
4. UNLISTEN `geo_updated` + release connection
5. Закрытие GeoMatcher, DB pool

```python
async def shutdown(self):
    self._running = False
    tasks = [t for t in self._worker_tasks if t and not t.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # ... close resources
```

**Правило:** `stop_grace_period` в docker-compose ≥ 30s.

### R-PR4: Heartbeat + Memory Fallback

Healthcheck должен мониторить RSS-память. Если потребление приближается к лимиту контейнера (1GB), Processor **ОБЯЗАН** применить graceful degradation (вызвать `gc.collect()`, отключить ONNX-модель, урезать LRU-кэши), чтобы предотвратить OOM Killer.

```python
def get_rss_mb(self) -> float:
    try:
        with open('/proc/self/status', 'r') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024  # kB → MB
    except Exception:
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # kB → MB

def check_memory(self) -> bool:
    rss_mb = self.get_rss_mb()
    return rss_mb < 850  # hard limit 1GB, warn at 850MB
```

```python
def _apply_memory_fallback(self):
    import gc
    gc.collect()
    if self.morph:
        try:
            self.morph.shrink_cache(max_size=5000)
            logger.info("Morphology LRU shrunk to 5000 (memory fallback)")
        except Exception as e:
            logger.warning(f"Failed to shrink Morphology cache: {e}")
```

### R-PR5: NLP-пайплайн — фиксированный порядок

```python
tokens = tokenize(raw_text)
lemmas = self.morph.lemmatize_tokens(tokens)
layer = self.layer_classifier.classify(lemmas)
entities = await self.matcher.find_geo(tokens=tokens, lemmas=lemmas)
# ... resolve conflicts ...
await self._insert_event_from_candidates(...)
```

**Правило:** Порядок шагов фиксирован. Нельзя менять последовательность.

### R-PR6: Retry with exponential backoff

```python
attempt < (8 if transient else 3)
delay = min(2 ** attempt, 30)
```

**Правило:** После исчерпания попыток → `mark_error` (статус 'error' в pending_events).

### R-PR7: is_promotional → random strategy

Промо/спам детектируется через `is_promotional()`. При срабатывании → `strategy=random`, сообщение НЕ игнорируется.

**Правило:** Даже спам-сообщения попадают на карту (random точка).

### R-PR8.1: Каждое сообщение отображается на фронтенде

Любое сообщение из `pending_events` ДОЛЖНО попасть в `events` и быть видимым на карте.

- **Распознанные локации:** normal strategy — точная геометрия
- **Нераспознанные / спам / пустые:** `strategy=random` — точка в зоне `question_overlay`

**Запрещено:**
- Дропать сообщения из-за отсутствия гео-кандидатов (`return None`)
- Отбрасывать сообщения после LLM-детекции спама — вместо этого `strategy=random`
- Фильтровать сообщения по длине текста, промо-характеру или слою

**Исключение:**
- Технические дубликаты (`ON CONFLICT DO NOTHING`).
- Сообщения с `event_time` вне 60-минутного окна (`-60min..+5min`) не выбираются воркерами и не отображаются на карте — они помечаются как `expired` (R-DB3, `clean_old_pending_events`).

### R-PR11: фильтр `event_time` в `_fetch_pending`

### R-PR9: SemanticResolver исключён из геометрии

`SemanticResolver` (и его наследники) НЕ ДОЛЖНЫ влиять на выбор стратегии геометрии или фильтрацию кандидатов. Его результаты используются ТОЛЬКО для отладки и логирования.

**Правило:** Если `SemanticResolver` вернул `strategy` или отфильтровал `geo_ids`, эти данные игнорируются при вызове `process_candidates_v2`. Передача стратегии как 4-й параметр (`p_hint`) сохранена для совместимости, но функция её игнорирует.

### R-PR10: process_candidates_v2 в PostGIS + Лимит Top-5

Python **ДОЛЖЕН ограничивать** количество кандидатов, передаваемых в `process_candidates_v2` (строго **Top-5** по `score`). Передача неограниченного списка запрещена, так как это вызывает деградацию БД.

```python
# R-PR10: hard cap at Top-5 to protect PostGIS process_candidates from CROSS JOIN blowup
geo_ids = geo_ids[:5]
geo_scores = geo_scores[:5]
geo_texts = geo_texts[:5]
```

**Вызов из Python (5 параметров, порог из .env):**

```sql
WITH pc AS (
    SELECT result_strategy, result_geom, result_matches,
           result_confidence, result_diagnostics
    FROM process_candidates_v2(
        $6::int[], $7::double precision[], $8::text[], $9::varchar,
        $10::double precision  -- geo_candidate_min_score (из .env)
    )
),
inserted AS (
    INSERT INTO events (...) SELECT ... FROM pc
    WHERE pc.result_strategy != 'random_null'
    ON CONFLICT (message_id, event_time) DO NOTHING
    RETURNING ...
)
```

**Правило:** Если v2 вернул `random_null` (geom=NULL), nlp_processor генерирует случайную точку через `_random_point()` и вставляет со strategy=`random` (R-PR22). Random точка НЕ генерируется внутри SQL.

### R-PR11: Pending events — двухфазный claim + очиститель

Воркер атомарно меняет статус на `processing` и фиксирует `locked_at`/`worker_id` внутри транзакции с `FOR UPDATE SKIP LOCKED`.

```python
"UPDATE pending_events "
"SET status = 'processing', locked_at = now(), worker_id = $1 "
"WHERE id = ("
"    SELECT id FROM pending_events "
"    WHERE status = 'pending' "
"      AND event_time >= now() - interval '60 minutes' "
"      AND event_time <= now() + interval '5 minutes' "
"    ORDER BY created_at "
"    LIMIT 1 "
"    FOR UPDATE SKIP LOCKED"
") "
"RETURNING id, message_id, text, event_time, photo_file_id"
```

**Почему не SELECT:**
- Классический `SELECT ... FOR UPDATE SKIP LOCKED` снимает блокировку при commit транзакции, а `status` остаётся `'pending'` — следующий воркер снова берёт ту же задачу (гонка).
- Двухфазный claim меняет `status → 'processing'` в той же транзакции: после commit задача недоступна другим воркерам, даже если обработка идёт долго.

**Очиститель зависших задач:**
Если воркер падает (SIGKILL/OOM/краш процесса), фоновый очиститель каждые 60с возвращает зависшие задачи (`locked_at < now() - 5 minutes`) в статус `pending`:

```sql
UPDATE pending_events
SET status = 'pending', locked_at = NULL, worker_id = NULL
WHERE status = 'processing'
  AND locked_at < now() - interval '5 minutes'
```

**Правило:**
- Multiple workers безопасно потребляют очередь без блокировок.
- Неучтённая ошибка воркера → `_requeue` с guard `AND status = 'processing'` (нельзя вернуть в `pending` задачу, уже помеченную `done`/`error`).
- Задача в статусе `processing` дольше 5 минут считается зависшей и реквоится очистителем (at-least-once семантика; дубликаты исключены idempotent INSERT, R-PR12).

### R-PR12: Idempotent INSERT

```sql
ON CONFLICT (message_id, event_time) DO NOTHING
```

**Правило:** Ретраи НЕ создают дубликатов.

### R-PR13: CTE pipeline — INSERT + meta + notify

```sql
WITH inserted AS (...),
     meta_upd AS (UPDATE events_meta SET version = version + 1 ...),
     notify_call AS (SELECT pg_notify('events_new', ...))
SELECT i.id, i.layer, i.strategy FROM inserted i
```

**Правило:** INSERT + meta-update + pg_notify — ОДИН SQL-запрос.

### R-PR14: LISTEN geo_updated для reindex

```python
await self._listen_conn.add_listener("geo_updated", self._on_geo_updated)
```

При `geo_updated` — перестройка PhoneticIndex. Reindex запускается как background task (не блокирует воркеры).

### R-PR15: Один asyncpg pool

```python
self.__pool = await asyncpg.create_pool(
    min_size=settings.db.pool_min_size,   # 1
    max_size=settings.db.pool_max_size,   # 10
    command_timeout=settings.db.command_timeout,  # 30s
)
```

**Правило:** Один пул на весь процесс. Connection release — через `async with pool.acquire()`.

### R-PR16: Morphology — LRU cache

```python
self.morph = Morphology()  # pymorphy3 + snowballstemmer, LRU 10k
```

**Правило:** Лемматизация кэшируется. Один экземпляр `Morphology` на процесс.

### R-PR17: PhoneticIndex — in-memory DAWG

```python
self.index = PhoneticIndex(self.morph)  # stem-based inverted index
```

**Правило:** Индекс строится при старте из `geo` таблицы. При `geo_updated` — инкрементальный reindex.

### R-PR18: GeoMatcher — sliding window 1-3 tokens

```python
entities = await self.matcher.find_geo(tokens=tokens, lemmas=lemmas)
# Два тира:
# Tier 1: stem exact (snowballstemmer)
# Tier 2: surface typo (rapidfuzz token_sort_ratio ≥ 0.85)
```

**Правило:** Матчер возвращает список `[{geo_id, score, text, type}]`.

### R-PR19: ProcessPoolExecutor для fuzzy matching

CPU-bound задачи (`rapidfuzz`) **ОБЯЗАНЫ** использовать `ProcessPoolExecutor`. Использование `ThreadPoolExecutor` **КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО** (из-за GIL).

```python
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(
    self._executor, _fuzzy_match, query, phrases, threshold
)
```

**Правило:** `_fuzzy_match` должна быть module-level функцией (picklable).

### R-PR20: Parameterized queries

Все SQL-запросы используют параметризацию ($1, $2, ...).

### R-PR21: Sanitize text

```python
@staticmethod
def _sanitize_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    return text.encode('utf-8', errors='replace').decode('utf-8')
```

**Правило:** Все тексты проходят UTF-8 sanitize перед вставкой в БД.

### R-PR22: Random point в зоне

```python
def _random_point(self):
    qo = settings.question_overlay
    r = radius * math.sqrt(random.random())
    theta = random.random() * 2 * math.pi
    return f"POINT({center_lng + r * math.cos(theta)} {center_lat + r * math.sin(theta)})"
```

**Правило:** Random точка генерируется внутри заданной зоны (question_overlay), не глобально.

### R-PR23: Structured logging

```python
if _LOG_FORMAT == 'json':
    from core.utils.logging_config import JSONFormatter
    _formatter = JSONFormatter()
else:
    _formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
```

### R-PR24: Log levels

- `DEBUG` — progress обработки, детали matching
- `INFO` — инициализация, успешная обработка
- `WARNING` — retry, fallback, потенциальные проблемы
- `ERROR` — ошибки обработки, DB-ошибки
- `CRITICAL` — crash воркера (auto-respawn)

### R-PR25: Settings из common/settings.py

Processor переиспользует `common/settings.py`. Собственные настройки минимальны.

### R-PR26: Environment variables — только DB credentials

| Переменная | Обязательна | Описание |
|-----------|-------------|----------|
| `POSTGRES_USER` | да | Пользователь PostgreSQL |
| `POSTGRES_PASSWORD` | да | Пароль PostgreSQL |
| `POSTGRES_DB` | нет | Имя БД (default: postgres) |

**Правило:** Всё остальное — хардкод в `common/settings.py`.

### R-PR27: Geometry-First (Отказ от семантических эвристик в геометрии)
Processor НЕ ДОЛЖЕН анализировать семантику текста (предлоги «между/от/до», отрицания «не/нет», контекстные списки) для выбора стратегии геометрии или фильтрации кандидатов.
Выбор стратегии (`single_match`, `intersection`, `street_segment`, `weighted_centroid`) определяется **ИСКЛЮЧИТЕЛЬНО** пространственными отношениями найденных кандидатов в функции `process_candidates()` (PostGIS).

### R-PR28: Docker Security

```yaml
# docker-compose.yml
nlp_processor:
  user: "1000:1000"
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL
  tmpfs:
    - /tmp:size=50m
```

**Правило:** Processor работает от non-root (UID 1000). `cap_drop: ALL`. tmpfs 50MB для NLP-операций.

**Алгоритм принятия решений (PostGIS):**
1. Наличие 2+ пересекающихся LINESTRING → `intersection` (POINT).
2. Наличие «главной» LINESTRING, имеющей пространственную связь (пересечение или `ST_DWithin` ≤ 50м) с 2+ другими кандидатами → `street_segment` (LINESTRING).
3. **Ни одна пара кандидатов не пересекается**, компактный кластер (scatter ≤ 1500м) → `weighted_centroid` (POINT). Если хотя бы одна пара пересекается → приоритет `intersection` или `street_segment`.
4. 1 кандидат → `single_match`.
5. 0 кандидатов или scatter > 1500м → `random`.

**Запрещено:**
- Pre-filter в Python на основе NLP-правил (удаление кандидатов из-за частицы «не»).
- Передача «хинтов» (hints) из `SemanticResolver` в `process_candidates`.
- Дроп кандидатов на основе контекста. Если `GeoMatcher` нашел топоним с `score >= threshold`, он передается в SQL.

**Принцип:** «Если матчер нашел топоним, он участвует в геометрическом расчете. Топология OSM сама расставит точки над i`.

### R-PR29: POS-filter для sliding-window (отключён по умолчанию)

POS-фильтр блокирует окна, состоящие **целиком** из ТОЧНО опознанных как НЕ-топоним токенов (VERB, ADVB, PREP, INTJ, CONJ, PRCL, INFN, PRTF, PRTS). OOV/GRND/NPRO пропускаются — pymorphy3 тегает неизвестные слова ошибочно.

```python
_BLOCKED_POS = frozenset({'VERB', 'ADVB', 'PREP', 'INTJ', 'CONJ', 'PRCL', 'INFN', 'PRTF', 'PRTS'})

# Блокировка: ТОЛЬКО если ВСЕ токены в окне заблокированы
if pos_enabled and clean_lemmas is not None:
    if all(lemma.pos in self._BLOCKED_POS for lemma in slice_l if lemma.pos):
        continue
```

**Настройка:** `GEO_ENABLE_POS_FILTER=true` в `.env`. По умолчанию **ВЫКЛ** — pymorphy3 тегает OOV-пропера как VERB/GRND, что даёт false negatives.

### R-PR30: Автоматическая генерация падежных форм (paradigm)

При построении PhoneticIndex для каждого geo-объекта с пропер-тегом (Geox/Name) генерируются все падежные формы через pymorphy3 lexeme. Формы добавляются как surface-фразы в Tier 2 индекс.

```python
# Только для пропров: иначе «Средняя» → «среднего/среднее» → false positive
has_proper = any('Geox' in str(p.tag) or 'Name' in str(p.tag) for p in parses)
if not has_proper:
    return []  # skip paradigm generation
```

**Эффект:** «Балковского» → «Балковская» без ручных алиасов в geo.csv.

---

## Антипаттерны (ЗАПРЕЩЕНО)

| Антипаттерн | Почему | Правило |
|-------------|--------|---------|
| NLP в parser | Нарушение разделения | R-PR1 |
| Синхронные SQL в воркере | Блокирует event loop | R-PR2 |
| INSERT без ON CONFLICT | Дубликаты | R-PR12 |
| Геометрия в Python | Неточность, нет PostGIS | R-PR10 |
| Ручной pool.release() без try | Утечка соединений | R-PR15 |
| Отсутствие SKIP LOCKED | Гонка воркеров | R-PR11 |
| Hardcoded credentials | Security | R-PR26 |
| ThreadPoolExecutor для rapidfuzz | GIL блокирует event loop | R-PR19 |
| Неограниченный список кандидатов | CROSS JOIN деградирует БД | R-PR10 |
| Отсутствие drain при shutdown | Потеря сообщений | R-PR3 |
| Дроп сообщений при отсутствии гео | Событие не попадает на карту | R-PR8.1 |

---

*Правила основаны на анализе кодовой базы nlp_processor/ — август 2026 (обновлено: Docker security, tmpfs, COPY --chown)*
