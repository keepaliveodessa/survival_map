"""Authentication API endpoints with JWT support"""
import time
import logging
from datetime import datetime, timezone

import jwt
from aiohttp import web

from common.settings import settings
from core.middlewares.auth import generate_jwt_tokens, verify_jwt_token
from core.utils.telegram_validation import validate_telegram_webapp_data

logger = logging.getLogger(__name__)


async def get_validation_config_handler(request: web.Request) -> web.Response:
    """
    GET /api/validation-config

    Returns Telegram validation configuration.
    Frontend calls this BEFORE loading map to know access rules.

    Response: {
        "telegram_webview_validation": true/false,
        "redirect_url": "..."
    }

    Security:
    - telegram_webview_validation=true: Only Telegram WebView allowed
    - telegram_webview_validation=false: Any webview allowed (dev mode)
    """
    validation_enabled = getattr(settings.app, 'telegram_webview_validation', True)
    redirect_url = getattr(settings.bot, 'redirect_url', None)
    
    # Ensure redirect_url is set when validation is enabled
    if validation_enabled and not redirect_url:
        redirect_url = ''
        logger.warning(
            "[Config] REDIRECT_URL not set but validation is enabled. "
            "Non-Telegram users will be redirected to gate fallback. "
            "Set REDIRECT_URL in .env."
        )
    
    response_data = {
        'telegram_webview_validation': validation_enabled,
        'redirect_url': redirect_url
    }
    
    logger.debug(f"[Config] Returning validation config: {response_data}")
    
    return web.json_response(response_data)


async def validate_init_handler(request: web.Request) -> web.Response:
    """
    Validate Telegram WebApp initData and issue JWT tokens.

    POST /api/validate-init
    Body: {"init_data": "..."}

    Response: {
        "valid": true/false,
        "user": {...},
        "access_token": "...",
        "refresh_token": "...",
        "expires_in": 900
    }
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {'valid': False, 'error': 'Invalid JSON body'},
            status=400
        )

    init_data = data.get('init_data')

    # Strict validation path
    if settings.app.telegram_webview_validation:
        if not isinstance(init_data, str) or not init_data.strip():
            return web.json_response(
                {'valid': False, 'error': 'Invalid init data'},
                status=401
            )

        is_valid, user_data = validate_telegram_webapp_data(
            init_data,
            settings.bot.token
        )
        
        if not is_valid:
            return web.json_response(
                {'valid': False, 'error': 'Invalid init data'},
                status=401
            )
    else:
        # Dev-bypass: включается ТОЛЬКО явным TELEGRAM_WEBVIEW_VALIDATION=false/0
        # (строгий парсер в common/settings.py). Защита production — в парсере:
        # любое другое/отсутствующее значение = валидация включена.
        logger.warning(
            f"validate-init in dev bypass mode from {request.remote} — "
            "issuing JWT for dev user (TELEGRAM_WEBVIEW_VALIDATION=False)"
        )
        user_data = {
            'id': '123456789',
            'first_name': 'Dev',
            'username': 'dev_user'
        }

    # Generate tokens (access + refresh + jti для хранения в БД)
    access_token, refresh_token, jti = generate_jwt_tokens(user_data)

    # Сохранить refresh-токен в БД для single-use контроля
    db = request.app.get('db')
    if db:
        try:
            await db.store_refresh_token(
                jti=jti,
                user_id=str(user_data.get('id')),
                expires_at=datetime.fromtimestamp(
                    int(time.time()) + settings.jwt.refresh_token_ttl,
                    tz=timezone.utc,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to store refresh token in DB: {e}", exc_info=True)
            # Не блокируем выдачу токенов при недоступности БД —
            # пользователь сможет войти, но rotation не будет работать до восстановления.

    return web.json_response({
        'valid': True,
        'user': user_data,
        'access_token': access_token,
        'refresh_token': refresh_token,
        'expires_in': settings.jwt.access_token_ttl
    })


async def gate_redirect_handler(request: web.Request) -> web.Response:
    """
    Log gate redirect for admin diagnostics.
    POST /api/gate-redirect
    Body: {"reason": "sdk_unavailable|no_initData|validation_failed|gate_error", "user_agent": "..."}
    """
    try:
        data = await request.json()
    except Exception:
        data = {}

    reason = data.get('reason', 'unknown')
    ua = data.get('user_agent', '')
    remote = request.remote or 'unknown'

    logger.warning(
        f"[Gate-Redirect] reason={reason} ua={ua} remote={remote}"
    )

    return web.Response(status=204)


async def refresh_token_handler(request: web.Request) -> web.Response:
    """
    Refresh access token using refresh token (Refresh Token Rotation).

    POST /api/auth/refresh
    Body: {"refresh_token": "..."}

    Response: {
        "access_token": "...",
        "refresh_token": "...",   ← новый refresh-токен (старый инвалидирован)
        "expires_in": 900
    }

    Security (RFC 6749 best practice):
    - Каждый refresh-токен single-use: при использовании помечается used_at.
    - Попытка повторного использования = признак кражи:
      инвалидируются ВСЕ токены пользователя, клиент вынужден авторизоваться заново.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {'error': 'Invalid JSON body'},
            status=400
        )

    refresh_token = data.get('refresh_token')
    
    if not refresh_token or not isinstance(refresh_token, str):
        return web.json_response(
            {'error': 'Missing or invalid refresh_token'},
            status=400
        )

    # Криптографическая проверка подписи и срока действия
    payload = verify_jwt_token(refresh_token, 'refresh')
    if not payload:
        return web.json_response(
            {'error': 'Invalid refresh token'},
            status=401
        )

    jti = payload.get('jti')
    user_id = str(payload.get('sub', ''))
    db = request.app.get('db')

    if db and jti:
        # Атомарно пометить как использованный и проверить что не был использован ранее
        was_valid = await db.consume_refresh_token(jti)
        if not was_valid:
            # Токен уже использован или отозван — возможная кража.
            # Инвалидируем все токены пользователя как меру безопасности.
            logger.warning(
                f"Refresh token reuse detected for user {user_id} "
                f"(jti={jti}) — revoking all tokens"
            )
            await db.revoke_all_user_tokens(user_id)
            return web.json_response(
                {'error': 'Refresh token already used or revoked'},
                status=401
            )
    elif not db:
        logger.warning(
            "DB not available during refresh — skipping single-use check. "
            "Rotation will not be enforced until DB is restored."
        )

    # Выдать новую пару токенов
    user_data = {
        'id': user_id,
        'first_name': payload.get('first_name', ''),
        'username': payload.get('username', ''),
    }
    new_access_token, new_refresh_token, new_jti = generate_jwt_tokens(user_data)

    # Сохранить новый refresh-токен в БД
    if db and new_jti:
        try:
            await db.store_refresh_token(
                jti=new_jti,
                user_id=user_id,
                expires_at=datetime.fromtimestamp(
                    int(time.time()) + settings.jwt.refresh_token_ttl,
                    tz=timezone.utc,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to store new refresh token in DB: {e}", exc_info=True)

    return web.json_response({
        'access_token': new_access_token,
        'refresh_token': new_refresh_token,
        'expires_in': settings.jwt.access_token_ttl
    })


async def init_cache(app: web.Application):
    """Initialize in-memory cache on app startup"""
    cache = app.get('cache')
    if cache:
        await cache.connect()
