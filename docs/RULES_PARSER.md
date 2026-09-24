# Rules — Parser Service v2.1

**Сервис:** `parser/` (kurigram + text preprocessing + photo download)
**Точка входа:** `python -m parser.monitoring`
**Порт:** 8765 (healthcheck only)
**Docker:** Multi-stage build, `COPY --chown=parser:parser`, non-root, LABEL

---

## 1. Архитектурные правила

### R-P1: Parser — ТОЛЬКО приём и предобработка

Parser НЕ выполняет NLP-классификацию, не ищет гео-объекты, не вычисляет геометрию. Его обязанность:

```
Telegram → strip_tail → preprocess_light → INSERT INTO pending_events
```

**Запрещено:**
- вызов `process_candidates()` или `geo_execute_scenario()`
- импорт `GeoMatcher`, `SemanticResolver`, `LayerClassifier`
- любой NLP-код (лемматизация, стемминг, токенизация)

**Исключение:** `text_preprocessor.py` — это **общий** модуль, переиспользуемый nlp_processor. Parser использует только `strip_tail` и `preprocess_light`.

### R-P2: Async архитектура — один event loop

Parser работает в одном asyncio event loop. Все операции — async/await. Синхронные вызовы (кроме `_write_heartbeat`) запрещены в hot path.

```python
# ✅ Правильно
await self.db.pool.execute(query, *args)

# ❌ Неправильно
import subprocess
subprocess.run([...])  # блокирует event loop
```

### R-P3: Graceful shutdown — drain before exit

При SIGTERM parser:
1. Перестаёт принимать новые сообщения (`self._running = False`)
2. Дренирует очередь (`pending_queue.join()`, timeout=20s)
3. Отменяет worker tasks
4. Останавливает Telegram client
5. Закрывает DB пул

```python
async def shutdown(self, drain_timeout: float = 20.0):
    self._running = False
    await asyncio.wait_for(self._pending_queue.join(), timeout=drain_timeout)
    # ... cancel tasks, close connections
```

**Правило:** `stop_grace_period` в docker-compose должен быть ≥ `drain_timeout + запас`.

### R-P4: Heartbeat healthcheck

Parser пишет timestamp в `/tmp/parser_heartbeat` каждую секунду. Docker healthcheck проверяет свежесть файла:

```bash
test -f /tmp/parser_heartbeat && [ $(( $(date +%s) - $(cat /tmp/parser_heartbeat) )) -lt 60 ]
```

**Правило:** Если heartbeat не обновляется >60s → контейнер перезапускается.

### R-P5: Idempotent Batch INSERT

Для снижения I/O нагрузки разрешен и рекомендуется **Batch INSERT** (`asyncpg.executemany`) с накоплением в `pending_queue`. Задержка батчинга не должна превышать 1-2 секунды. Идемпотентность (`ON CONFLICT DO NOTHING`) обязательна.

```python
# ❌ Неправильно: одиночный INSERT на каждое сообщение
await conn.execute(
    "INSERT INTO pending_events ... VALUES ($1, $2, $3) "
    "ON CONFLICT ... DO NOTHING",
    ...
)

# ✅ Правильно: Batch INSERT через executemany
await conn.executemany(
    "INSERT INTO pending_events (message_id, text, event_time) "
    "VALUES ($1, $2, $3) "
    "ON CONFLICT (message_id, event_time) DO NOTHING",
    batch
)
```

**Правило:** Задержка батчинга ≤ 2 секунд. Ретраи и backfill НЕ создают дубликатов.

### R-P6: preprocess_light СОХРАНЯЕТ регистр

`preprocess_light()` используется для `description` (отображается на фронтенде). Регистр, emoji, пунктуация — сохраняются.

### R-P7: strip_tail — МЯГКАЯ очистка

`strip_tail()` удаляет служебный хвост ("сообщить", "подписаться"), но НЕ трогает основной контент. Маркеры — регистронезависимые.

### R-P8: Unicode normalization

Украинские буквы → русские:
- `і`, `І` → `и`, `И`
- `ї`, `Ї` → `и`, `И`
- `є`, `Є` → `е`, `Е`

Украинские окончания → русские:
- `івська` → `овская`
- `ський` → `ский`

**Правило:** Нормализация применяется в `preprocess_light()` и `clean()`.

### R-P9: Emoji handling

Emoji УДАЛЯЮТСЯ только перед токенизацией/матчингом (`strip_emoji()`). В `description` emoji СОХРАНЯЮТСЯ.

### R-P10: Промо/спам детекция

`is_promotional()` — высокоточный детектор (ссылки, Telegram-хендлы, призывы к подписке). При срабатывании → `strategy=random` (случайная точка), но сообщение НЕ игнорируется.

**Правило:** Precision важнее recall — мягкая реклама намеренно не ловится.

### R-P11: Один пул соединений

Parser использует один `asyncpg.Pool` на весь процесс. Пул создаётся при старте, закрывается при shutdown.

Размеры пула берутся из `common/settings.py` (DatabaseConfig dataclass):

```python
self.__pool = await asyncpg.create_pool(
    min_size=settings.db.pool_min_size,   # 1
    max_size=settings.db.pool_max_size,   # 10
    command_timeout=settings.db.command_timeout,  # 30s
)
```

**Правило:** `max_size ≤ max_connections / количество_сервисов` (см. R-DB15).
3 сервиса × `max_size=10` = 30, что ≤ `max_connections=50`.

### R-P12: Parameterized queries

Все SQL-запросы используют параметризацию ($1, $2, ...). Конкатенация строк ЗАПРЕЩЕНА.

### R-P13: Connection release

При использовании `pool.acquire()` соединение автоматически возвращается в пул при выходе из `async with`. Ручной `release()` — только для long-lived соединений (LISTEN/NOTIFY).

### R-P14: LISTEN/NOTIFY для фото

Parser слушает два канала:
- `photo_download` — скачивание фото после обработки events
- `events_cleaned` — удаление физических файлов фото

**Правило:** Каждый listener имеет own connection из пула + backoff reconnect.

### R-P15: Path traversal protection

Скачивание фото блокирует path traversal.

### R-P16: Symlink protection

Если `final_path` — symlink, он удаляется перед скачиванием.

### R-P17: Sanitize text

Все тексты проходят `utf-8` sanitize.

### R-P18: Proxy configuration

SOCKS5/HTTP proxy настраивается через settings (не env для sensitive данных).

### R-P19: Structured logging

Parser использует JSON-формат (по умолчанию) или текстовый — через `settings.app.log_format`.

### R-P20: Log levels

- `DEBUG` — progress загрузки истории, детали обработки
- `INFO` — инициализация, успешная обработка, shutdown
- `WARNING` — retry, fallback, потенциальные проблемы
- `ERROR` — ошибки обработки, DB-ошибки
- `CRITICAL` — crash воркера (auto-respawn)

### R-P21: Settings из common/settings.py

Parser переиспользует `common/settings.py` для всех настроек.

### R-P22: Environment variables

Только sensitive/per-deployment настройки через env.

### R-P23: Минимальное покрытие

Parser — простой сервис, основной фокус на интеграционных тестах.

### R-P24: Mock Telegram messages

Тесты используют mock-объекты для `pyrogram.Message`.

### R-P25: Docker Security

```yaml
# docker-compose.yml
parser:
  user: "1000:1000"
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL
  tmpfs:
    - /tmp:size=10m
```

**Правило:** Parser работает от non-root (UID 1000). `cap_drop: ALL`. tmpfs для temp-файлов.

### R-P26: Cleanup stale photos (70 min)

Parser обязан содержать страховку от потери pg_notify: файлы фото старше 70 минут
удаляются **только если на них больше не ссылается ни одно событие в `events`**
(DB-aware). Фото «живого» события при этом НЕ трогается — раньше безусловное
удаление рвало картинку, пока событие ещё существовало (битый `<img>` + 401 на
nginx API-fallback).

```python
async def _cleanup_stale_photos(self):
    """Удалять файлы старше 70 минут, на которые больше НЕ ссылается ни одно
    событие (страховка от потери pg_notify)."""
    cutoff = time.time() - 70 * 60
    media_dir = Path(self.events_media_dir)
    if not media_dir.exists() or self.db is None or self.db.pool is None:
        return

    candidates = [
        f for f in media_dir.iterdir()
        if f.is_file() and f.stat().st_mtime < cutoff
    ]
    if not candidates:
        return

    try:
        async with self.db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT photo_url FROM events WHERE photo_url LIKE $1",
                "/media/events/%",
            )
    except Exception as e:
        logger.warning(f"stale photo DB check failed, skipping cleanup: {e}")
        return

    referenced = {row['photo_url'] for row in rows}

    deleted = 0
    for f in candidates:
        url = f"/media/events/{f.name}"
        if url in referenced:
            continue
        try:
            f.unlink()
            deleted += 1
        except FileNotFoundError:
            pass   # идемпотентно
        except Exception as e:
            logger.warning(f"stale photo delete failed: {f}: {e}")
    if deleted:
        logger.info(f"Stale photos cleaned (unreferenced): {deleted}")
```

**Правило:** Запуск раз в 5 минут в главном цикле. `FileNotFoundError` проглатывается
(идемпотентность). Orphan-файлы (событие удалено, `pg_notify` потерян) удаляются на
следующей итерации — строки в `events` уже нет, значит фото больше не нужно.
При недоступности БД проход пропускается целиком (безопаснее НЕ удалять, чем
удалить живое фото).

---

## Антипаттерны (ЗАПРЕЩЕНО)

| Антипаттерн | Почему | Правило |
|-------------|--------|---------|
| NLP-код в parser | Нарушение разделения обязанностей | R-P1 |
| Синхронные вызовы в hot path | Блокирует event loop | R-P2 |
| SQL-конкатенация | SQL injection | R-P12 |
| `clean()` для description | Потеря регистра/emoji | R-P6 |
| Hardcoded credentials | Security | R-P22 |
| Отсутствие drain при shutdown | Потеря сообщений | R-P3 |
| Ручной `pool.release()` без try/except | Утечка соединений | R-P13 |
| Игнорирование `ON CONFLICT` | Дубликаты при ретраях | R-P5 |
| Одиночный INSERT вместо Batch | Высокая I/O нагрузка | R-P5 |

---

*Правила основаны на анализе кодовой базы parser/ — август 2026 (обновлено: Docker security, COPY --chown)*
