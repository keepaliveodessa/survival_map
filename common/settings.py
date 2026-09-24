from dataclasses import dataclass, field
from environs import Env
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def _parse_strict_bool(env: Env, var_name: str, default: bool = True) -> bool:
    """Secure by Default парсер булевых env-переменных.

    Возвращает default (True), если переменная не задана.
    Возвращает False ТОЛЬКО если значение явно равно 'false' или '0'
    (регистронезависимо). Во всех остальных случаях — True.
    """
    val = env.str(var_name, default=None)
    if val is None:
        return default

    is_disabled = val.strip().lower() in ('false', '0')
    if is_disabled:
        logger.warning(
            f"SECURITY RISK: {var_name} is explicitly set to '{val}' — "
            "dev-bypass ENABLED."
        )

    return not is_disabled


# Ключевые слова слоёв — канонические словоформы (не стемы).
# LayerClassifier лемматизирует и ключи, и токены сообщения через mawo_pymorphy3,
# поэтому все падежи/числа словоформ совпадают автоматически.
#
# Порядок ключей задаёт приоритет классификации: первый совпавший слой
# выигрывает (см. parser/layer_classifier.py). 'pig' — fallback без ключей.
DEFAULT_LAYER_KEYWORDS: dict[str, tuple] = {
    'bus': (
        'автобус',
        'бус',
        'хайс',
        'спринтер',
        'рено',
        'фольксваген',
        'хёндай',
        'Хундай',
        'вито',
        'вольксваген',
        'Кадди',
        'сталкер',
        'транспортёр',
        'h1', 'h2', 'h3', 'h4', 'h5',
        'т5', 'т4', 'т3', 'т2', 'т1',
        'н1', 'н2', 'н3', 'н4', 'н5',
        # pymorphy лемматизирует «бус»→«бусы», но «буса»/«бусик»→самостоятельные
        # леммы ⇒ косвенные/слэнговые формы не совпадали. Добавлены явно.
        'буса', 'бусик',
    ),
    'cops': (
        'коп',
        'полиция',
        'мусор',
        'мусара',
        'люстра',
        'мигалка',
        'патруль',
        'экипаж',
        'мент',
        'менты',
        'полицейский',
        'полицай',
        'police',
        'мусорня',
        'мусорской',
    ),
    'traffic': (
        'дтп',
        'авария',
        'пробка',
        'затор',
        'светофор',
        'блокпост',
        'пост',
        'бп',
        'б/п'
    ),
    'pig': (),
}

# Порядок приоритета (исключая fallback 'pig').
LAYER_PRIORITY: tuple = tuple(k for k in DEFAULT_LAYER_KEYWORDS if k != 'pig')


@dataclass
class DatabaseConfig:
    """PostgreSQL — прямое подключение (без PgBouncer)."""
    host: str = "postgres"
    port: int = 5432  # may be overridden by POSTGRES_PORT
    database: str = "postgres"
    user: str = "postgres"
    # Пароль не имеет дефолта: резолвится из env POSTGRES_PASSWORD в load_settings
    # через _resolve_postgres_password с безусловным fail-fast (M-1): отсутствие/
    # пустое значение, небезопасные дефолты ('postgres', 'admin', ...) и длина
    # < 8 символов роняют сервис на старте — по образцу _resolve_jwt_secret (R-C8).
    password: str = ""
    # Прямое подключение: каждый коннект = backend process в postgres.
    # 3 сервиса × pool_max_size=10 = 30 max. Under max_connections=50.
    # min_size=1: asyncpg opens min_size соединений eagerly — 3 total at startup
    # (not 15 with the old min_size=5). Sufficient for 5 msg/min load.
    pool_min_size: int = 1
    pool_max_size: int = 10
    # Command timeout для SQL-запроса. Slowest observed query: 3.6s.
    # 30s timeout leaves 8x margin. Aligns with R-C15 (core) and R-DB14.
    command_timeout: int = 30


@dataclass
class AppConfig:
    host: str = "0.0.0.0"  # nosec B104 — bind all interfaces (nginx reverse proxy)
    port: int = 8080
    telegram_webview_validation: bool = True
    # Логирование (main.py, parser/monitoring.py читают эти поля)
    log_level: str = "INFO"
    log_format: str = "json"  # json | text
    # CORS: пустой кортеж = same-origin only (nginx проксирует фронтенд →
    # CORS не нужен). При явном списке доменов app_factory включает CORS.
    allowed_origins: tuple = ()


@dataclass
class BotConfig:
    token: str
    # channel_id читается из env CHANNEL_ID (см. load_settings) и пробрасывается
    # в контейнер parser через docker-compose.yml. Дефолт "-1002050105527"
    # оставлен только как fallback в load_settings (и в дефолте compose),
    # не в dataclass — чтобы избежать появления production-ID в git-истории
    # при смене деплоймента.
    channel_id: str
    webapp_url: Optional[str] = None
    redirect_url: Optional[str] = None


@dataclass
class JWTConfig:
    secret: str
    access_token_ttl: int = 900  # 15 minutes
    refresh_token_ttl: int = 86400  # 24 hours
    algorithm: str = "HS256"


@dataclass
class SimilarityConfig:
    """Параметры sliding-window линкера гео-объектов и LayerClassifier.

    Используются GeoMatcher (nlp_processor/geo_matcher.py) и LayerClassifier.
    Только поля, реально читаемые матчером; рудименты старого Python-резолвера
    (midpoint/lemma-fuzzy/tier-пороги) удалены — геометрию решает PostGIS
    (process_candidates_v2), распознавание — стем-индекс (Tier 1).
    """
    # Финальный top-K результатов find_geo().
    max_entities: int = 5

    # Длиннее этого порога (символов) сообщение не считается релевантной локацией.
    max_text_length: int = 380

    # Порог fuzz.ratio для surface-орфо-корректора (Tier 2 в _link_span, 0-1).
    # 0.80: пропускает слабые совпадения (0.80–0.85) — не проходят как confident.
    # Точные стем-матчи (Tier 1, score varies) не затрагивают.
    surface_typo_threshold: float = 0.80

    # Sliding-window: максимальный размер окна (токенов) при генерации кандидатов.
    # Окно 1..max_sliding_window охватывает улицы из 1, 2 или 3 слов.
    max_sliding_window: int = 3

    # Бонус к score для кандидатов, которым предшествует локационный предлог
    # ("на", "по", "в" и т.п.). Помогает при дедупе когда оба матча за одну улицу.
    prepositional_boost: float = 0.05

    # Включить POS-фильтрацию (pymorphy3) для sliding-window кандидатов.
    # Безопасно ВКЛ (TASK 1): фильтр отбрасывает окно только если ВСЕ его токены
    # уверенно опознаны как не-топоним (VERB/ADVB/GRND/...), при условии что ни
    # один стем окна не присутствует в geo-индексе (stem-rescue). OOV-пропера
    # (Гаванная→GRND, героив→GRND) спасаются стем-совпадением; UNKN/пустой POS
    # всегда проходят. Предлоги вообще не доходят до фильтра — они границы окон.
    enable_pos_filter: bool = True

    # Токены-пунктуация: отфильтровываются из tokens до поиска (_strip_noise).
    punctuation_tokens: tuple = (
        '#', '/', ',', '.', '(', ')', '!', '?', '-', '«', '»', '"', ':', ';',
    )

    def get_layer_keywords(self, layer: str) -> tuple:
        return DEFAULT_LAYER_KEYWORDS.get(layer, ())


@dataclass
class GeoConfig:
    """Пороги гео-арбитража process_candidates_v2.

    Все пространственные лимиты вынесены из SQL-функции в .env.
    Изменение значений НЕ требует пересборки Docker-образов.
    """
    # Минимальный score кандидата для передачи в process_candidates_v2 (0-1).
    # Кандидаты с score < этого порога отбрасываются ДО геометрического арбитража.
    candidate_min_score: float = 0.80

    # Единый буфер пересечения (метры) для построения графа связных компонент.
    # ST_DWithin(geom_a, geom_b, buffer_m). Одинаков для Point, LineString, Polygon.
    intersection_buffer_m: float = 100.0

    # Максимальный scatter (метры) для weighted_centroid (TASK 3 / Hard Constraint 5).
    # Если scatter > этого значения, гипотеза отклоняется → процесс_candidates_v2
    # выбирает single_match с максимальным score, вместо точки посередине.
    weighted_centroid_max_scatter_m: float = 500.0


@dataclass
class ParserConfig:
    """Параметры parser-сервиса (kurigram, photo download)."""

    # Сколько сообщений тянуть из истории канала при старте парсера.
    history_limit: int = 100

    # Каталог хранения медиафайлов (фотографии событий).
    events_media_dir: str = "/media/events"

    # Макс. длина текста (символов) после preprocess_light для вставки в pending_events.
    max_text_length: int = 380

    # SOCKS5/HTTP proxy для pyrogram.
    socks5_host: Optional[str] = None
    proxy_host: Optional[str] = None
    proxy_scheme: str = "socks5"
    proxy_port: int = 1080


@dataclass
class NlpProcessorConfig:
    """Параметры nlp_processor-сервиса (NLP pipeline)."""

    # Число конкурентных воркеров, потребляющих из pending_events (SKIP LOCKED).
    worker_concurrency: int = 5

    # Polling interval (сек) при пустой очереди.
    poll_interval: float = 0.5


@dataclass
class QuestionOverlayConfig:
    """Границы зоны для событий без точной привязки к местности (круг)"""
    center_lon: float = 30.83135  # Центр по долготе
    center_lat: float = 46.49804  # Центр по широте
    radius: float = 0.04  # Радиус круга (в градусах)

    @property
    def center(self) -> tuple:
        return (self.center_lat, self.center_lon)


@dataclass
class LayerConfig:
    cops: tuple = field(default_factory=lambda: DEFAULT_LAYER_KEYWORDS['cops'])
    bus: tuple = field(default_factory=lambda: DEFAULT_LAYER_KEYWORDS['bus'])
    traffic: tuple = field(default_factory=lambda: DEFAULT_LAYER_KEYWORDS['traffic'])
    pig: tuple = field(default_factory=lambda: DEFAULT_LAYER_KEYWORDS['pig'])

    def as_dict(self) -> dict:
        """Слой → tuple ключевых слов. Порядок соответствует LAYER_PRIORITY + 'pig'."""
        return {layer: getattr(self, layer) for layer in DEFAULT_LAYER_KEYWORDS}


@dataclass
class Settings:
    app: AppConfig
    db: DatabaseConfig
    bot: BotConfig
    jwt: Optional[JWTConfig] = None
    similarity: SimilarityConfig = field(default_factory=SimilarityConfig)
    geo: GeoConfig = field(default_factory=GeoConfig)
    layers: LayerConfig = field(default_factory=LayerConfig)
    parser: ParserConfig = field(default_factory=ParserConfig)
    nlp_processor: NlpProcessorConfig = field(default_factory=NlpProcessorConfig)
    question_overlay: QuestionOverlayConfig = field(default_factory=QuestionOverlayConfig)


def _resolve_postgres_password(env: Env) -> str:
    """Validate POSTGRES_PASSWORD with UNCONDITIONAL fail-fast on insecure values.

    Security requirements (M-1: раньше проверки срабатывали только при
    ENVIRONMENT=production, но ENVIRONMENT нигде не задавался — fail-open):
    - значение ОБЯЗАТЕЛЬНО: отсутствие/пустая строка → RuntimeError;
    - небезопасные дефолты ('postgres', 'password', ...) → RuntimeError;
    - минимальная длина 8 символов → иначе RuntimeError.

    По образцу _resolve_jwt_secret (R-C8): сервис не стартует с небезопасной
    конфигурацией. docker-compose дублирует защиту на уровне оркестрации
    (${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env} — проверяет unset/empty).
    """
    password = env.str("POSTGRES_PASSWORD", None)

    if not password:
        raise RuntimeError(
            "FATAL: POSTGRES_PASSWORD is required in environment. "
            "Set a strong password (min 8 chars) in .env."
        )

    insecure_passwords = {
        "postgres",
        "password",
        "123456",
        "admin",
        "root",
        "changeme",
        "change-me",
        "default",
    }

    # Небезопасные дефолты запрещены всегда (не только в production).
    if password.lower() in insecure_passwords:
        raise RuntimeError(
            "FATAL: POSTGRES_PASSWORD uses an insecure default — "
            "set a strong password (min 8 chars)."
        )

    # Minimum length check
    if len(password) < 8:
        raise RuntimeError(
            f"FATAL: POSTGRES_PASSWORD too short (got {len(password)} chars, need >= 8)."
        )

    return password


def _resolve_jwt_secret(env: Env) -> str:
    secret = env.str("JWT_SECRET", None)
    if not secret:
        raise RuntimeError("FATAL: JWT_SECRET is required in environment (R-C8).")
    insecure_defaults = {
        "your-secret-key",
        "your-secret-key-change-in-production",
        "your-secret-key-change-in-production-min-32-chars",
        "secret",
        "changeme",
        "change-me",
    }
    if secret.lower() in insecure_defaults or secret.startswith("your-secret"):
        raise RuntimeError(
            "FATAL: JWT_SECRET is a placeholder — set a real secret (R-C8)."
        )
    if len(secret) < 32:
        raise RuntimeError(
            f"FATAL: JWT_SECRET must be >= 32 chars (got {len(secret)}) (R-C8)."
        )
    return secret


def load_settings(env_path: Optional[str] = None, require_jwt: bool = True) -> Settings:
    """Load settings — env читается ТОЛЬКО для credentials/per-deployment URL.

    Всё остальное — хардкодные дефолты в соответствующих `@dataclass`. Чтобы
    изменить калибровку матчера / параметры БД / прокси и т.п., правится
    `common/settings.py` напрямую (не env).

    Keep-list env: BOT_TOKEN, WEBAPP_URL, REDIRECT_URL, CHANNEL_ID.
    JWT_SECRET — обязателен при require_jwt=True (R-C8).
    """
    env = Env()
    env.read_env(env_path)

    try:
        telegram_webview_validation = _parse_strict_bool(
            env, "TELEGRAM_WEBVIEW_VALIDATION", True
        )
        bot_token = env.str("BOT_TOKEN", "")
        try:
            jwt_secret = _resolve_jwt_secret(env)
            jwt_config = JWTConfig(secret=jwt_secret)
        except RuntimeError:
            if require_jwt:
                raise
            jwt_config = None

        return Settings(
            app=AppConfig(
                telegram_webview_validation=telegram_webview_validation,
            ),
            db=DatabaseConfig(
                host=env.str("POSTGRES_HOST", "postgres"),
                port=env.int("POSTGRES_PORT", 5432),
                user=env.str("POSTGRES_USER", "postgres"),
                password=_resolve_postgres_password(env),
                database=env.str("POSTGRES_DB", "postgres"),
            ),
            bot=BotConfig(
                token=bot_token,
                channel_id=env.str("CHANNEL_ID", "-1002050105527"),
                webapp_url=env.str("WEBAPP_URL", None),
                redirect_url=env.str("REDIRECT_URL", None),
            ),
            jwt=jwt_config,
            similarity=SimilarityConfig(),
            layers=LayerConfig(),
            parser=ParserConfig(
                socks5_host=env.str("PROXY_HOST", None),
                proxy_port=env.int("PROXY_PORT", 1080),
                proxy_scheme=env.str("PROXY_SCHEME", "socks5"),
                history_limit=env.int("PARSER_HISTORY_LIMIT", 100),
            ),
            question_overlay=QuestionOverlayConfig(),
        )
    except Exception as e:
        raise ValueError(f"Configuration error: {e}")


settings = load_settings(require_jwt=False)
