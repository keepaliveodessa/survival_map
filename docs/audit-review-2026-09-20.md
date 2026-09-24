# Аудит проекта Survival Map — 2026-09-20

Полное ревью кодовой базы: архитектура, backend (core/parser/nlp_processor/common),
frontend (web), PostgreSQL-схема, инфраструктура (Docker, nginx, CI/CD), тесты и
безопасность.

---

## 1. Резюме

Проект — зрелый, хорошо спроектированный микросервисный стек с **нетипично
сильной для своего масштаба инженерной культурой**: очереди с гаранлиями
at-least-once, circuit breaker, graceful shutdown повсюду, watermark-синхронизация
клиентов, стабильно работающий CI с security-сканерами. Код читаемый, документация
(docs/RULES_*.md, ~2100 строк) соответствует коду.

**Проверка на живой машине:**

| Проверка | Результат |
|---|---|
| pytest (backend) | ✅ **281 passed**, 72 skipped (тяжёлые deps: mawo_pymorphy3, asyncpg — в CI ставятся) |
| `tsc --noEmit` (frontend) | ✅ 0 ошибок |
| jest (frontend) | ✅ 41/41 (3 suite) |
| eslint | ✅ 0 errors, 3 warnings (false positives) |
| bandit `-ll` | ✅ No issues |

**Оценка: 8.5/10.** Главные проблемы — не в коде, а в **конфигурации деплоя**
(CHANNEL_ID фактически не настраивается через .env) и **fail-open проверках
пароля БД в production**.

---

## 2. Сильные стороны

### Архитектура
- Чистый поток данных `parser → pending_events → nlp_processor → events (PostGIS)
  → pg_notify → core → WS → карта`; сети Docker изолированы (`db: internal`),
  наружу только web:80.
- Очередь `pending_events`: двухфазный claim (`FOR UPDATE SKIP LOCKED` + статус
  `processing` в одной транзакции), фоновый очиститель зависших задач, requeue
  при крахе воркера, backpressure с прямой записью в БД при переполнении
  in-memory очереди (at-least-once вместо drop).
- TTL событий через часовые партиции + pg_cron, с двухуровневой защитой
  (`clean_old_events` + safety-net `manage_event_partitions` + `partition_overflow`
  алерт).
- Watermark-синхронизация WS/REST клиентов по `message_id` (стабилен между
  рестартами БД) с детекцией устаревшего кэша (`resync_required`) — частая точка
  боли для подобных систем, здесь решена корректно.

### Безопасность
- Telegram initData: HMAC-SHA256 по спецификации, `hmac.compare_digest`,
  freshness-проверка `auth_date`, ограничение длины, bound строковых полей.
- JWT: refresh rotation single-use + детекция переиспользования с отзывом всех
  токенов пользователя (RFC 6749 best practice).
- Secure-by-default: `TELEGRAM_WEBVIEW_VALIDATION` — strict-парсер, `false`/`0`
  единственный триггер dev-bypass; матричный тест 4 комбинаций env в CI.
- Fail-fast на секретах: `JWT_SECRET` ≥32 симв., плейсхолдеры отклоняются;
  `POSTGRES_PASSWORD` без дефолта в compose (`:?`).
- Path traversal: `media.py` — basename-only, resolve() + relative_to (symlink
  escape закрыт); парсер генерирует имена сам.
- Edge: CSP (строгий script-src), X-Frame-Options, nosniff, rate-limit на nginx
  (api 10r/s, auth 1r/s) + app-level, `server_tokens off`, блокировка `*.js.map`,
  real_ip только от приватных CIDR.
- CI: bandit + pip-audit + hadolint + trivy (HIGH/CRITICAL exit-code 1) + eslint
  security plugin.
- Контейнеры: `cap_drop: ALL`, non-root UID 1000, `no-new-privileges`, tmpfs
  noexec/nosuid, лимиты CPU/RAM, ротация логов. Секреты не в git (`.env`,
  `*.session` в .gitignore; api_id/api_hash — проверка G-15 в CI).

### Надёжность
- Graceful shutdown с таймаутами во всех сервисах; адекватные комментарии о
  гонках сигналов aiogram/aiohttp (решены через `handle_signals=False`).
- Circuit breaker (pybreaker + свой), retry с backoff и разделением
  transient/non-transient ошибок, healthcheck'и с heartbeat-файлом.
- Адаптивный пул воркеров (2..8) по размеру очереди; supervisor перезапускает
  упавших воркеров; при памяти > порога — graceful degradation.
- Фронт: reconnect с экспоненциальным backoff + jitter, self-heal по
  visibility/online/Telegram-activated, ping/pong с допусками, snapshot-режим,
  server-clock anchor для TTL.

---

## 3. Находки

### 🔴 Высокий приоритет

**H-1. CHANNEL_ID не пробрасывается в контейнеры — настройка через .env не работает.**
- `docker-compose.yml` не передаёт `CHANNEL_ID` ни в `parser`, ни в `core`.
- При этом `.env.example` предлагает его настраивать, а
  `common/settings.py:395` читает `env.str("CHANNEL_ID", "-1002050105527")`.
- Итог: любое значение из `.env` **молча игнорируется**, парсер всегда работает
  с захардкоженным fallback'ом. При смене канала приложение продолжит читать
  старый канал без каких-либо ошибок.
- Комментарии в `settings.py` («CHANNEL_ID захардкожен в BotConfig, не env»)
  противоречат коду — либо убирать env-чтение, либо добавлять переменную в
  compose. Рекомендация: добавить `CHANNEL_ID: ${CHANNEL_ID}` в environment
  parser/core и привести комментарии в соответствие.

### 🟡 Средний приоритет

**M-1. Production-проверки пароля БД — fail-open.**
- `_resolve_postgres_password` (`common/settings.py:307,323`) поднимает
  RuntimeError только при `ENVIRONMENT=production`, но переменная `ENVIRONMENT`
  не задана ни в docker-compose.yml, ни в .env.example — проверка никогда не
  сработает, в реальном проде будет только warning.
- Кроме того, docstring `DatabaseConfig.password` («пустая строка — явно
  невалидный дефолт») не соответствует коду: `load_settings` всегда подставляет
  `postgres`. Рекомендация: добавить `ENVIRONMENT` в compose/.env.example либо
  применять строгую проверку безусловно (мин. длина — всегда, insecure-дефолты —
  всегда raise).

**M-2. Узкое покрытие тестами фронтенда.**
- Тестируются только 3 модуля (sanitizeUrl, createPopupContent, store) — 41 тест.
  Самая сложная логика без тестов: `websocket.ts` (reconnect/heartbeat/snapshot —
  ~450 строк протокольного кода), `token-manager.ts` (refresh rotation),
  `local_cache.ts`, `vector-layer.ts`. CI закрепляет узкий набор
  (`--testPathPatterns="(sanitizeUrl|createPopupContent|store)"`).
- Рекомендация: хотя бы тесты на watermark-логику и обработку
  `resync_required`/`events_snapshot_end` в WS-менеджере.

**M-3. Нет gate на покрытие бэкенда.**
- pytest-cov установлен, но CI гоняет `pytest -q` без `--cov` и `fail-under`.
  281 тест — хорошо, но регрессия покрытия никем не отслеживается.

### 🟢 Низкий приоритет

**L-1. `parser/monitoring.py` — хрупкая связка start()/shutdown().**
- `self._stale_photo_task` создаётся только внутри `start()` (строка 271), но
  безусловно читается в `shutdown()` (строка 635). Если `start()` упадёт до
  создания задачи, `finally: await parser.shutdown()` бросит AttributeError и
  замаскирует исходную ошибку. Аналогично: оба photo-слушателя получают
  одноразовый `asyncio.Event()` как shutdown_event, который никогда не
  set() — остановка держится только на cancel(). Использовать `getattr(self,
  '_stale_photo_task', None)` и единый shutdown_event.

**L-2. `ZoneInfo('Europe/Kiev')`** — легаси-алиас (совр. `Europe/Kyiv`), плюс
`tzdata==2024.1` в parser/requirements устарел. Работает, но стоит обновить.

**L-3. Мёртвая метрика:** `ws_broadcast_duration_seconds...observe(0)` —
гистограмма всегда пишет 0; либо замерять реально, либо убрать.

**L-4. Dead code:** `jwt_auth_middleware` всё ещё читает cookie `session_token`
как fallback — по комментарию в app_factory cookie нигде не ставится. Убрать
во избежание ложного ощущения работающей cookie-аутентификации.

**L-5. Непрошитые `:latest` образы** мониторинга (prometheus, grafana, loki,
promtail, postgres-exporter) — основные сервисы запинены, стек наблюдаемости
нет. Репродуктивность деплоя с профилем `monitoring` не гарантирована.

**L-6. WS `subscribe_layers` не валидирует имена слоёв** (принимает любые
строки; нехешируемые элементы поймаются общим except). Влияние нулевое —
фильтр просто не совпадёт, но whitelist из 4 слоёв был бы чище.

**L-7. Деплой-джоб в CI — заглушка** («implement as needed»), а `deploy:local`
делает `docker compose down` перед `up` — окно даунтайма на каждый деплой.
Для single-host приемлемо, но `up -d --build` без `down` + healthcheck-гейт
дали бы то же без пауз в выдаче.

### ℹ️ Наблюдения (без действий)

- SQL во всём бэкенде параметризован — f-строки собирают только плейсхолдеры
  `$N`; bandit B608 корректно исключён осознанно.
- eslint warnings — false positives (`security/detect-non-literal-regexp` в
  map.ts, `detect-non-literal-fs-filename` в telegram/integration.ts — это
  Telegram WebApp API, не node fs).
- «exception while scanning» у bandit на nlp_processor/* — несовместимость bandit
  1.7.10 с локальным Python 3.14; в CI (3.11) сканирование проходит.
- Схема events: constraint-миграции (`UPDATE strategy ...`) в init-скрипте
  выполняются только при создании тома — осознанный trade-off, задокументирован.
- Рабочая копия содержит staged-удаления старых отчётов/бакапов (~5.5k строк
  мусора) — завершить начальную чистку коммитом.
- Сложная цепочка rewrite'ов `/js/.../__v__/TOKEN` в nginx.conf работает, но
  хрупка; webpack contenthash в именах файлов дал бы то же кэширование без
  кастомного рерайтинга.

---

## 4. Оценка по областям

| Область | Оценка | Комментарий |
|---|---|---|
| Архитектура | 9/10 | Чистые границы сервисов, осознанные гаранлии очереди |
| Backend (core) | 9/10 | Auth, WS, кэширование — образцово; мелкие dead code |
| Parser | 8/10 | Надёжный пайплайн; хрупкая связка start/shutdown |
| Processor (NLP) | 8/10 | Двухфазный claim, breaker, дедуп SQL;PostGIS-арбитраж в БД |
| Frontend | 8/10 | Store/WS/offline-first сильные; покрытие тестами узкое |
| PostgreSQL | 9/10 | Партиции, TTL, триггеры, таймауты по ролям |
| Безопасность | 9/10 | Одна из лучших практик; fail-open пароля (M-1) — минус |
| Инфраструктура | 8/10 | Compose/CI зрелые; CHANNEL_ID (H-1), :latest мониторинга |
| Тестирование | 7/10 | 281 backend + 41 frontend; нет cov-gate, узкий фронт |
| Документация | 9/10 | RULES_*.md соответствуют коду, README точный |

**Итог: 8.5/10**

---

## 5. Рекомендованный порядок работ

1. **H-1** — пробросить `CHANNEL_ID` в compose (1 строка) + синхронизировать
   комментарии `settings.py`.
2. **M-1** — сделать проверку пароля безусловной или завести `ENVIRONMENT` в
   compose/.env.example.
3. **M-2/M-3** — тесты на `websocket.ts` (reconnect, snapshot, resync) и
   `--cov --cov-fail-under` в CI.
4. **L-1..L-4** — точечные правки parser shutdown, метрики, dead code.
5. **L-5** — запинить образы monitoring-профиля.
6. Закоммитить уже staged-удаления отчётов/бакапов.
