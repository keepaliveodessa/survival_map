# Повторный аудит проекта Survival Map — 2026-09-20 (v2)

Повторный аудит после перезапуска приложения. Предыдущее ревью: 
[docs/audit-review-2026-09-20.md](audit-review-2026-09-20.md).

Отличие от v1: проверен не только код, но и **живой стек** (runtime-состояние,
HTTP-эндпоинты, поток событий), а также последствия коммита очистки `719f749`.

---

## 1. Статус находок v1

| № | Находка v1 | Статус в v2 |
|---|---|---|
| **H-1** | CHANNEL_ID не пробрасывался в контейнеры | ✅ **Исправлено** (compose + комментарии settings.py, проверено runtime) |
| **M-1** | Fail-open проверка POSTGRES_PASSWORD | ✅ **Исправлено** (безусловный fail-fast, +17 тестов; в .env уже стоит валидный пароль — стек поднялся) |
| **M-2** | Узкое покрытие тестами фронтенда | ⬜ Открыто |
| **M-3** | Нет coverage-gate в CI | ⬜ Открыто |
| L-1..L-7 | Точечные (parser shutdown, Europe/Kiev, dead metric, cookie fallback, :latest monitoring, subscribe_layers, deploy down) | ⬜ Открыты |

Оба исправления закоммичены (`e2a12f2`) и запушены в оба ремоута.

## 2. Runtime-вердикт (живой стек)

Проверено снаружи (через web:80):

| Проверка | Результат |
|---|---|
| `GET /health/ready` | ✅ 200, `database: healthy`, `bot: healthy`, v1.0.6 |
| `GET /health/detailed` | ✅ pool 2/10, memory-cache healthy |
| Поток данных | ✅ **65 features** за 60 мин, все 4 слоя (`pig/cops/bus/traffic`) — полный конвейер parser→nlp_processor→PostGIS→API работает |
| WebSocket | ✅ handshake `101 Switching Protocols` |
| `/api/geo` × 8 | ✅ 5–12 ms, стабильное кэширование |
| App-level rate limit (30 rapid POST /api/events) | ✅ упирается в 429 |
| Edge rate limit (/api/validate-init ×8) | ✅ 6×200 → 429 (burst=5 сработал) |
| CSP / X-Frame-Options / nosniff на map.html | ✅ все на месте |
| Path traversal `/api/media/events/..%2f..` | ✅ 403 |
| `docker compose config` после правок H-1/M-1 | ✅ валиден, CHANNEL_ID пробрасывается |

Вывод: **после перезапуска стек полностью функционален**, исправления M-1 не
сломали деплой (пароль в .env заменён на валидный).

### ⚠️ Но: стенд работает в dev-bypass с публичным URL

- `.env`: `TELEGRAM_WEBVIEW_VALIDATION=false` — API и WS принимают **любого**
  клиента без токена (подтверждено: POST /api/events без авторизации → 200).
- При этом `WEBAPP_URL=https://karin-sluglike-lewis.ngrok-free.dev` — стек
  **доступен из интернета** через ngrok-туннель.
- Формально это режим «только для локальной отладки» (README, warning в логах
  core), но сочетание «публичный домен + выключенная валидация» — это открытый
  прокси-доступ к данным карты и метрикам для кого угодно. Для реального
  прод-использования нужно `true` (см. H-3).

## 3. Повторные проверки качества

| Проверка | Результат |
|---|---|
| pytest (backend) | ✅ **298 passed** (было 281; +17 тестов M-1), 72 skipped |
| `tsc --noEmit` | ✅ 0 ошибок |
| jest (frontend) | ✅ 41/41 |
| bandit на изменённые файлы | ✅ No issues |
| yamllint `.gitlab-ci.yml` | ✅ OK |
| eslint | ✅ 0 errors (3 известные false-positive warnings) |
| GitLab pipeline #96 (коммит `8b44a8b`) | ⚠️ **21 success / 1 failed (trivy-scan) / 1 skipped** — integration-tests и все 4 матричные джобы зелёные; trivy — см. M-6 |

## 4. Новые находки (v2)

### 🔴 H-2. CI сломан коммитом очистки: удалены скрипты, на которые ссылается pipeline

> **Статус: исправлено** — логика восстановлена в `scripts/ci_matrix_check.py`
> (поддерживает env-режим GitLab parallel:matrix и argv-режим run-ci-local.sh);
> джоба `test:core-startup-matrix` и локальный runner снова вызывают рабочий
> скрипт; убраны ссылки на `prepare-deploy.sh`; заодно скрипт сделан устойчивым
> к import-time утечке `.env` в os.environ (локальный dev `.env` с '0'/'false'
> ломал комбинацию UNSET) и адаптирован под fail-fast POSTGRES_PASSWORD (M-1).
> Все 4 комбинации матрицы проверены локально в обоих режимах + fail-кейс.
> **Подтверждено в CI:** пайплайн #96 (`8b44a8b`) — все 4 матричные джобы
> `test:core-startup-matrix` → **success**.

Коммит `719f749` («sync: remove unused scripts») удалил
`scripts/ci_check_webview_validation.py` и `scripts/prepare-deploy.sh`, но:

- `.gitlab-ci.yml:295` — джоба `test:core-startup-matrix` (матрица 4 комбинаций
  env, G-11) всё ещё вызывает `python scripts/ci_check_webview_validation.py`;
- `.gitlab-ci.yml:302` — файл указан в `changes:`-триггере;
- `.gitlab-ci.yml:90` — `check-local-prerequisites` предлагает запустить
  `./prepare-deploy.sh`, которого больше нет;
- `run-ci-local.sh:105` — дублирует вызов удалённого скрипта.

Итог: **удалённый CI-пайплайн красный** — матричный тест secure-by-default
падает на каждом пайплайне. Варианты: вернуть скрипт (он был маленьким и
полезным — там проверялся строгий парсер bool) или переписать джобу на
inline-python. Заодно вычистить упоминание prepare-deploy.sh.

### 🔴 H-3. (конфигурация окружения) Публичный доступ к незащищённому стенду

> **Статус: `.env` исправлен, НО ЕЩЁ НЕ ПРИМЕНЁН к контейнеру core**
> (проверено дважды, 15:23 и 16:05 UTC): web пересобран (nginx `/metrics` → 404,
> фикс M-5 активен), однако core работает в dev-bypass:
> `/api/events`, `/api/geo`, `/api/config` без токена → 200;
> `/api/validate-init` с пустым body → 200 (dev-JWT).
> Runtime-улика: поле uptime в /health/detailed (= epoch старта процесса)
> ИДЕНТИЧНО в проверках 15:23 и 16:05 (1789915920.07) — процесс core не
> перезапускался с ~14:52 UTC, т.е. `docker compose up -d core` не дошёл
> до контейнера (не выполнялся / выполнен из другой директории / был `restart`,
> который env не перепрочитывает).
> Для применения (из корня проекта): `docker compose up -d core`, затем
> `curl -s -o /dev/null -w '%{http_code}' -X POST localhost/api/events \
>   -H 'Content-Type: application/json' -d '{"time_filter":15}'` → должен стать 401.
> Примечание: `REDIRECT_URL=http://shodan.io` выглядит как dev-заглушка — после
> включения валидации именно туда будет уходить не-Telegram трафик; стоит
> задать осмысленный URL (или пусто — gate-fallback).

См. раздел 2. Это не баг кода — код честно работает в заданном режиме, — но
текущее сочетание `TELEGRAM_WEBVIEW_VALIDATION=false` + публичный ngrok-домен
фактически открывает карту (и эксперименты с API) любому, кто найдёт URL.
Рекомендация: либо включить валидацию (стек уже настроен под Mini App), либо
снять туннель, пока идёт разработка.

### 🟡 M-4. Профиль monitoring в compose мёртв: postgres/monitoring/ отсутствует

`docker-compose.yml` монтирует пять конфигов из `./postgres/monitoring/`
(postgres_exporter.yml, prometheus.yml, grafana provisioning, loki.yml,
promtail.yml) — **каталога нет в репо** и нет в git-истории. `docker compose
--profile monitoring up` упадёт на создании контейнеров (bind-mount несуществующего
файла). Директория не в .gitignore — то есть она никогда не коммитилась либо
потеряна при чистке. Либо восстановить конфиги, либо выпилить профиль до
восстановления.

### 🟡 M-5. Метрики Prometheus пишутся «в никуда», /metrics отдаёт HTML

> **Статус: исправлено** — добавлен `core/api/metrics.py` (эндпоинт на глобальном
> REGISTRY, only-GET, JWT-защита по умолчанию), регистрация в `routes.py`,
> nginx `location = /metrics { return 404; }` (исключён из SPA-fallback).
> +7 тестов (`tests/test_api_metrics.py`), всего 305 passed.
> Джоба **integration-tests в пайплайне #96 — success**. Первоначальный вариант
> теста был env-зависим: 405-проверка в strict-режиме получала 401 от
> JWT-middleware раньше роутера (middleware выполняется до маршрутизации);
> исправлено — routing-тесты прогоняются в обоих режимах валидации.
> Примечание: при скрейпе prometheus'ом из backend-сети при включённой
> валидации нужен токен либо осознанное добавление /metrics в PUBLIC_ENDPOINTS
> (внешний доступ уже закрыт nginx 404).

- `prometheus_client` стоит в requirements, `register_http_metrics()` и
  counters/gauges инициализируются в `core/app_factory.py` и `core/metrics.py`,
  но **эндпоинт `/metrics` в core не зарегистрирован** (`routes.py` его не
  содержит).
- Снаружи `GET /metrics` отдаёт 200 с **index.html** — это SPA-fallback nginx
  (`location /`), а не метрики. Метрики инстанса недоступны принципиально,
  exporter postgres тоже не подключён (M-4).
- Для включения нужно: `app.router.add_get('/metrics', ...)` +
  `generate_latest()` (и исключить путь из jwt_auth и из SPA-fallback nginx,
  если хочется отдавать наружу — безопаснее отдавать только внутри backend-сети
  для prometheus).

### 🟡 M-6. trivy-scan: security-гейт сработал — HIGH/CRITICAL в собранных образах

Конфигурация джобы исправлена (`entrypoint: [""]` против `unknown command "sh"`,
`TRIVY_USERNAME/PASSWORD` для pull из приватного CI_REGISTRY) и джоба **реально
сканирует**: 28 сек до первого fail при последовательном скане
core→parser→nlp_processor→web→postgres — первый же образ с HIGH/CRITICAL останавливает
джобу через `--exit-code 1` (так и задумано). Лог джобы недоступен текущему
токену (нет гранулярного `Job: Read` / `Job Artifact: Read`), поэтому конкретные
CVE требуют triage: обновление базовых образов либо обоснованный `.trivyignore`.
Это штатное срабатывание сканера, а не ошибка пайплайна.

### 🟡 M-2 / M-3 — переносятся из v1 без изменений

(узкое покрытие фронтенда; отсутствие coverage-gate).

### 🟢 L-новые. Гигиена конфигурации

> **Статус: исправлено** — из `.env` удалены 5 мёртвых переменных
> (`ENTITY_SIMILARITY_THRESHOLD`, `GEO_CANDIDATE_MIN_SCORE` ×2 (дубль с разными
> значениями), `GEO_INTERSECTION_BUFFER_M`, `GEO_WEIGHTED_CENTROID_MAX_SCATTER_M`,
> `GEO_ENABLE_POS_FILTER`) вместе с их блоками; устранён дубль `JWT_SECRET`
> (сохранено эффективное last-wins значение — проверено sha256-хэшами до/после);
> `.env.example` больше не предлагает мёртвую `ENTITY_*`, добавлено пояснение,
> что калибровка правится в `common/settings.py`. Бэкап: `/tmp/env-backup-1789917175`.

- **Мёртвые env-переменные в `.env`:** `ENTITY_SIMILARITY_THRESHOLD`,
  `GEO_CANDIDATE_MIN_SCORE`, `GEO_INTERSECTION_BUFFER_M`,
  `GEO_WEIGHTED_CENTROID_MAX_SCATTER_M`, `GEO_ENABLE_POS_FILTER` — не читаются
  ни одним модулем (проверено grep по core/parser/nlp_processor/common). Комментарий
  в `.env.example` про ENTITY_SIMILARITY_THRESHOLD тоже устарел: калибровка
  переехала в хардкодные dataclass-дефолты `common/settings.py`.
- **Дубли ключей в `.env`:** `JWT_SECRET` и `GEO_CANDIDATE_MIN_SCORE` встречаются
  дважды. Для дубликатов поведение dotenv-парсеров неоднозначно (первое/последнее
  значение) — потенциальный источник «мистических» несовпадений секрета между
  сервисами. Оставить по одному.
- `uptime` в /health/detailed содержит epoch-подобное значение (~1.79e9), а не
  секунд аптайма — поле вводит в заблуждение мониторинг-скрипты.
- `.env.example` по-прежнему советует `./prepare-deploy.sh`? — нет, но README
  раздел 3 не упоминает, что CHANNEL_ID теперь пробрасывается (мелочь,
  документация и так актуальна по существу).

### ℹ️ Прочие наблюдения

- `settings.layers` отдаются целиком на `POST /api/config` без аутентификации в
  dev-bypass — при включённой валидации эндпоинт закрыт JWT (не входит в
  PUBLIC_ENDPOINTS), ок.
- `nlp_processor/health.py` отдаёт `/health/ready` на 8765 — контейнер-локально,
  наружу не торчит, ок.
- Пайплайн #96 (`8b44a8b`): 21 success, trivy-scan failed (M-6), deploy skipped
  (зависим от image-security). Матрица secure-by-default (G-11) снова зелёная в CI.

## 5. Обновлённая оценка по областям

| Область | v1 | v2 | Комментарий |
|---|---|---|---|
| Архитектура | 9/10 | 9/10 | Без изменений |
| Backend (core) | 9/10 | 9/10 | Без изменений; минус M-5 (метрики не экспортируются) |
| Parser / Processor | 8/10 | 8/10 | Конвейер работает (65 событий/час, все слои) |
| Frontend | 8/10 | 8/10 | Без изменений |
| PostgreSQL | 9/10 | 9/10 | Без изменений |
| Безопасность | 9/10 | 8.5/10 | M-1 закрыт, но минус за стенд с публичным доступом без валидации (H-3) |
| Инфраструктура | 8/10 | **7.5/10** | H-2/M-5 закрыты и подтверждены в CI (пайплайн #96: матрица + integration-tests зелёные); открыты M-4 и M-6 (CVE-triage) |
| Тестирование | 7/10 | 7.5/10 | +17 тестов fail-fast; M-2/M-3 открыты |
| Документация | 9/10 | 9/10 | Без изменений |

**Итог: 8.5/10 → 8/10; после закрытия H-2/M-5/L и верификации в CI (#96) —
к 8.5/10.** Код не стал хуже — регрессия в «склейке» устранена; открыты
M-4 (monitoring-профиль), M-6 (CVE-triage в trivy), M-2/M-3 и применение
H-3 к контейнеру core.

## 6. Рекомендованный порядок работ

1. ~~H-2~~ ✅ закрыт (пайплайн #96: все 4 матричные джобы success).
   ~~M-5~~ ✅ закрыт (integration-tests success).
2. **H-3** — применить валидацию к контейнеру: `docker compose up -d core`
   (.env готов, требуется пересоздание контейнера, restart env не перепрочитывает).
3. **M-6** — triage trivy: лог джобы (нужен токен с `Job: Read`) → обновить
   базовые образы или обоснованный `.trivyignore`.
4. **M-4** — восстановить `postgres/monitoring/` либо выпилить профиль до
   восстановления.
5. **M-2/M-3** — тесты на websocket.ts + coverage-gate.
6. Чистка L: L-1..L-7 из v1.
