# Правила проекта — Survival Map v2.1

Единый документ правил для всех микросервисов. Каждый контейнер — независимый сервис.

---

## Глобальные правила

### G-1: Single Source of Truth

PostgreSQL — единственное надежное хранилище состояния.

### G-2: Минимум зависимостей

Каждый сервис содержит только необходимые ему библиотеки.
Shared-код живёт в `common/`; каждый контейнер копирует только свой пакет + `common/`.

### G-3: Fail Fast

Валидация конфигурации при старте. Ошибки должны быть громкими.

### G-4: Observability First

Структурированные логи (JSON). Метрики удалены (Phase 1).

### G-5: Security by Default

Запрет хардкод-секретов. Строгая валидация входных данных.

### G-6: Idempotency

Все операции записи идемпотентны (`ON CONFLICT`).

### G-7: Resource Limits

Лимиты CPU/RAM в `docker-compose.yml` обязательны.

### G-8: Docker Security

Контейнеры работают от non-root UID 1000. `cap_drop: ALL` для всех сервисов.
`tmpfs` для ephemeral данных. Multi-stage builds для минимальных образов.

### G-9: Secrets Isolation

`JWT_SECRET`, `POSTGRES_PASSWORD`, `BOT_TOKEN` — ТОЛЬКО в `.env`.
Never commit. Never log. Never echo. См. `.env.example` для шаблона.

### G-10: No Stale Code

Неиспользуемые `.js` файлы, устаревшие скрипты, мёртвые модули — удаляются
при обнаружении. Не держать «на всякий случай».

### G-11: Единый settings.py

Все Python-сервисы переиспользуют `common/settings.py`.

**Булевы env-переменные парсятся строго** через `_parse_strict_bool`
(по умолчанию `True`; `False` — только при явном `'false'`/`'0'`). Никаких
inline-`os.getenv`/`env.bool` для auth-флагов за пределами `settings.py`.

### G-17: common/ не зависит от сервисов

`common/` не должен импортировать никакие сервисные пакеты (`core`, `parser`, `nlp_processor`).
Сервисы могут импортировать `common/`, но не могут импортировать друг друга.

### G-14: NLP-словари и зависимости — только в образе

Все NLP-словари (pymorphy3, snowballstemmer) и зависимости устанавливаются ОДИН РАЗ
при сборке Dockerfile.nlp_processor (через pip/apt на этапе builder). В рантайме ничего
не скачивается.

Запрещено:
- Скачивание словарей или моделей при старте контейнера
- HTTP-вызовы к внешним репозиториям моделей в рантайме

### G-15 / G-16: Сессия без авторизации

`api_id/api_hash` только в `gen_session.py`. Рантайм-авторизация запрещена.

---

## Поток данных

```
Telegram (MTProto) → parser (kurigram + strip_tail + preprocess_light) → pending_events
    → nlp_processor (tokenize → lemmatize → classify → find_geo → process_candidates_v2)
    → postgres (PostGIS, pg_notify, LISTEN/NOTIFY)
    → core (aiohttp REST + WebSocket + aiogram bot)
    → web (nginx + Leaflet/MapLibre GL, PWA)
    → Browser / Telegram WebView
```

---

## Правила по сервисам

Подробные правила каждого сервиса — в отдельных файлах:

| Сервис | Файл правил | Точка входа | Порт |
|--------|-------------|-------------|------|
| **postgres** | [RULES_POSTGRES.md](RULES_POSTGRES.md) | `postgres -c config_file=...` | 5432 (internal) |
| **parser** | [RULES_PARSER.md](RULES_PARSER.md) | `python -m parser.monitoring` | heartbeat only |
| **nlp_processor** | [RULES_NLP_PROCESSOR.md](RULES_NLP_PROCESSOR.md) | `python -m nlp_processor.main` | heartbeat only |
| **core** | [RULES_CORE.md](RULES_CORE.md) | `python main.py` | 8080 (internal) |
| **web** | [RULES_WEB.md](RULES_WEB.md) | nginx | **80 (external)** |

---

## Антипаттерны (ЗАПРЕЩЕНО — глобальные)

| Антипаттерн | Почему | Правило |
|-------------|--------|---------|
| NLP-код в parser/core | Нарушение разделения | G-1, R-P1, R-C1 |
| Синхронные вызовы в async hot path | Блокирует event loop | R-P2, R-C2 |
| SQL-конкатенация | SQL injection | R-P12, R-C17 |
| INSERT без ON CONFLICT | Дубликаты при ретраях | R-P5, R-PR12 |
| Дроп сообщений при спаме/нет гео | Событие не попадает на карту | R-PR8.1 |
| Отсутствие drain при shutdown | Потеря сообщений | R-P3, R-C3, R-PR3 |
| Hardcoded credentials | Security | G-11 |
| Внешние порты кроме 80 | Security | G-6 |
| dev-bypass в production | Security | R-C10 |
| Эфемерная генерация JWT_SECRET | Массовый logout при деплое | R-C8 |
| Ручной DELETE по времени в events | Lock contention, ломает BRIN | R-DB2, R-DB3 |
| ThreadPoolExecutor для rapidfuzz | GIL блокирует event loop | R-PR19 |
| Семантические эвристики текста для геометрии | Нарушение Geometry-First, неточность | R-PR27, R-DB8 |
| Docker контейнер от root | Security | G-8 |
| Отсутствие cap_drop в compose | Security | G-8 |
| Stale .js файлы в web/js/ | Мусор, путаница | G-10 |
| Скачивание NLP-словарей в рантайме | Неопределённость, network dependency | G-14 |

---

*Правила проекта — август 2026 (обновлено: Docker hardening, security isolation, stale code cleanup)*
