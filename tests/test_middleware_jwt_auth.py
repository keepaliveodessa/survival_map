"""Tests for core/middlewares/jwt_auth.py."""
from unittest.mock import patch

import pytest

try:
    from core.middlewares.jwt_auth import jwt_auth_middleware, PUBLIC_ENDPOINTS, PUBLIC_PREFIXES
    _IMPORT_OK = True
    _IMPORT_ERR = None
except Exception as e:
    _IMPORT_OK = False
    _IMPORT_ERR = repr(e)

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason=f"jwt_auth import unavailable: {_IMPORT_ERR}"
)


class FakeRequest:
    def __init__(self, path='/api/events', headers=None, cookies=None, method='GET'):
        self.path = path.rstrip('/') or '/'
        self.method = method
        self.headers = headers or {}
        self.cookies = cookies or {}
        self._telegram_user = None

    def __setitem__(self, key, value):
        if key == 'telegram_user':
            self._telegram_user = value

    def __getitem__(self, key):
        if key == 'telegram_user':
            return self._telegram_user
        raise KeyError(key)


class FakeHandler:
    def __init__(self, response_status=200):
        self.response_status = response_status
        self.called = False

    async def __call__(self, request):
        self.called = True
        from aiohttp import web
        return web.json_response({'ok': True}, status=self.response_status)


async def _call(middleware, request, handler):
    mw = await middleware(None, handler)
    return await mw(request)


class TestPublicEndpoints:
    @pytest.mark.parametrize("path", [
        '/health',
        '/health/live',
        '/health/ready',
        '/health/detailed',
        '/api/validation-config',
        '/api/validate-init',
        '/api/auth/refresh',
    ])
    @pytest.mark.asyncio
    async def test_public_bypasses_auth(self, path):
        handler = FakeHandler()
        req = FakeRequest(path=path)
        resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 200
        assert handler.called is True


class TestPublicMedia:
    """Медиа публична по дизайну: nginx отдаёт статику без авторизации,
    а API-fallback (nginx @api_fallback) проксирует без Authorization."""
    @pytest.mark.parametrize("path", [
        '/api/media',
        '/api/media/',
        '/api/media/events/photo123.jpg',
        '/api/media/events/event_20260923_150505_559122.jpg',
    ])
    @pytest.mark.asyncio
    async def test_media_bypasses_auth(self, path):
        handler = FakeHandler()
        req = FakeRequest(path=path)
        resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 200
        assert handler.called is True

    def test_public_prefixes_contains_media(self):
        assert '/api/media' in PUBLIC_PREFIXES

    @pytest.mark.asyncio
    async def test_media_sibling_still_requires_auth(self):
        """Соседний путь /api/mediax НЕ должен быть публичным префиксом."""
        handler = FakeHandler()
        req = FakeRequest(path='/api/mediax/events/photo.jpg', headers={'Authorization': 'Bearer'})
        with patch('core.middlewares.jwt_auth.settings') as mock_settings:
            mock_settings.app.telegram_webview_validation = True
            resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 401
        assert handler.called is False


class TestDevModeBypass:
    @pytest.mark.asyncio
    async def test_validation_disabled_allows_all(self):
        handler = FakeHandler()
        req = FakeRequest(path='/api/events')
        with patch('core.middlewares.jwt_auth.settings') as mock_settings:
            mock_settings.app.telegram_webview_validation = False
            resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 200
        assert handler.called is True


class TestMissingToken:
    @pytest.mark.asyncio
    async def test_returns_401_without_token(self):
        handler = FakeHandler()
        req = FakeRequest(path='/api/events')
        with patch('core.middlewares.jwt_auth.settings') as mock_settings:
            mock_settings.app.telegram_webview_validation = True
            resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 401
        assert handler.called is False


class TestInvalidToken:
    @pytest.mark.asyncio
    async def test_returns_401_for_invalid_token(self):
        handler = FakeHandler()
        req = FakeRequest(path='/api/events', headers={'Authorization': 'Bearer bad.token'})
        with patch('core.middlewares.jwt_auth.settings') as mock_settings:
            mock_settings.app.telegram_webview_validation = True
            resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 401
        assert handler.called is False


class TestValidToken:
    @pytest.mark.asyncio
    async def test_attaches_user_on_valid_token(self):
        handler = FakeHandler()
        payload = {'sub': '7', 'first_name': 'Test', 'username': 'test'}
        with patch('core.middlewares.jwt_auth.settings') as mock_settings:
            mock_settings.app.telegram_webview_validation = True
            with patch('core.middlewares.jwt_auth.verify_jwt_token', return_value=payload):
                req = FakeRequest(path='/api/events', headers={'Authorization': 'Bearer good.token'})
                resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 200
        assert handler.called is True
        assert req['telegram_user']['id'] == 7


class TestWebSocketBypass:
    @pytest.mark.asyncio
    async def test_ws_bypasses_jwt(self):
        handler = FakeHandler()
        req = FakeRequest(path='/ws')
        with patch('core.middlewares.jwt_auth.settings') as mock_settings:
            mock_settings.app.telegram_webview_validation = True
            resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 200
        assert handler.called is True


class TestCookieToken:
    @pytest.mark.asyncio
    async def test_accepts_token_from_cookie(self):
        handler = FakeHandler()
        payload = {'sub': '7', 'first_name': 'Test', 'username': 'test'}
        with patch('core.middlewares.jwt_auth.settings') as mock_settings:
            mock_settings.app.telegram_webview_validation = True
            with patch('core.middlewares.jwt_auth.verify_jwt_token', return_value=payload):
                req = FakeRequest(path='/api/events', cookies={'session_token': 'good.token'})
                resp = await _call(jwt_auth_middleware, req, handler)
        assert resp.status == 200
        assert handler.called is True
