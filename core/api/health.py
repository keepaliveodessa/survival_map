"""Health check endpoints for monitoring and orchestration"""
import logging
import time
from aiohttp import web
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# TTL-кэш результата DB-probe: probe'ы летят каждые 15-30с со стороны
# docker/nginx healthcheck — не имеет смысла каждый раз гонять SELECT 1.
# Кэшируем результат на _DB_PROBE_TTL_SEC секунд; если за это время БД упадёт,
# probe увидит это с задержкой до TTL, что приемлемо.
_DB_PROBE_TTL_SEC = 5.0
_db_probe_cache: dict = {'ts': 0.0, 'ok': False, 'error': None}


async def _check_db(db_request, *, use_cache: bool) -> tuple[bool, str]:
    """Проверка PostgreSQL. Возвращает (ok, message).

    Args:
        use_cache: если True, возвращает результат не старше TTL без актуального
            SELECT 1 (для /health liveness). False — всегда актуальный probe
            (для /health/ready, чтобы LB не отправлял трафик на падающий DB).
    """
    now = time.monotonic()
    if use_cache and now - _db_probe_cache['ts'] < _DB_PROBE_TTL_SEC:
        if _db_probe_cache['ok']:
            return True, 'Connected (cached)'
        return False, _db_probe_cache['error'] or 'Not connected (cached)'

    try:
        if db_request and db_request.db.is_connected:
            await db_request.db.fetchval('SELECT 1')
            _db_probe_cache.update({'ts': now, 'ok': True, 'error': None})
            return True, 'Connected'
        msg = 'Not connected'
        _db_probe_cache.update({'ts': now, 'ok': False, 'error': msg})
        return False, msg
    except Exception as e:
        msg = f'Error: {e}'
        _db_probe_cache.update({'ts': now, 'ok': False, 'error': msg})
        logger.error(f"Database health check failed: {e}")
        return False, msg


async def health_live_handler(request: web.Request):
    """
    Liveness probe - checks if app is running
    Returns 200 if the application process is alive
    
    Use for: Kubernetes liveness probe, load balancer health checks
    """
    utc_time = datetime.now(timezone.utc)
    return web.json_response({
        'status': 'alive',
        'timestamp': utc_time.isoformat()
    })


async def health_ready_handler(request: web.Request):
    """
    Readiness probe - checks if app is ready to serve traffic
    Validates all critical dependencies:
    - PostgreSQL connection
    - Bot initialization
    
    Returns:
        200 - Ready to serve traffic
        503 - Not ready (dependencies unavailable)
    
    Use for: Kubernetes readiness probe, deployment validation
    """
    db_request = request.app.get('db')
    cache = request.app.get('cache')
    
    checks = {
        'database': {'status': 'unknown', 'message': ''},
        'bot': {'status': 'unknown', 'message': ''},
        'overall': 'healthy'
    }
    
    # Check PostgreSQL — readiness probe должна быть актуальной (без кэша),
    # иначе LB направит трафик на падающий DB в течение TTL-окна.
    db_ok, db_msg = await _check_db(db_request, use_cache=False)
    if db_ok:
        checks['database'] = {'status': 'healthy', 'message': db_msg}
    else:
        checks['database'] = {'status': 'unhealthy', 'message': db_msg}
        checks['overall'] = 'unhealthy'
    
    # Check Telegram Bot (basic check)
    try:
        bot = request.app.get('bot')
        if bot:
            # Check if bot token is present
            checks['bot'] = {'status': 'healthy', 'message': 'Initialized'}
        else:
            checks['bot'] = {'status': 'unhealthy', 'message': 'Not initialized'}
            checks['overall'] = 'unhealthy'
    except Exception as e:
        checks['bot'] = {'status': 'unhealthy', 'message': f'Error: {str(e)}'}
        checks['overall'] = 'unhealthy'
        logger.error(f"Bot health check failed: {e}")
    
    # Return response
    utc_time = datetime.now(timezone.utc)
    status_code = 200 if checks['overall'] == 'healthy' else 503

    return web.json_response({
        'status': checks['overall'],
        'timestamp': utc_time.isoformat(),
        'checks': checks
    }, status=status_code)


async def health_detailed_handler(request: web.Request):
    """
    Detailed health check with metrics
    Returns comprehensive system information
    
    Use for: Monitoring dashboards, debugging
    """
    db_request = request.app.get('db')
    cache = request.app.get('cache')

    utc_time = datetime.now(timezone.utc)
    health_data = {
        'status': 'healthy',
        'timestamp': utc_time.isoformat(),
        'uptime': request.app.get('start_time', 0),  # Track app start time
        'version': '1.0.6',
        'checks': {}
    }
    
    # Database metrics
    try:
        if db_request and db_request.db.pool:
            pool = db_request.db.pool
            health_data['checks']['database'] = {
                'status': 'healthy',
                'pool_size': pool.get_size(),
                'pool_free': pool.get_size() - pool.get_idle_size(),
                'pool_max': pool.get_max_size()
            }
    except Exception as e:
        health_data['checks']['database'] = {
            'status': 'unhealthy',
            'error': str(e)
        }
        health_data['status'] = 'unhealthy'
    
    # Cache metrics
    try:
        if cache:
            cache_stats = await cache.get_stats()
            health_data['checks']['cache'] = {
                'status': 'healthy',
                'backend': cache_stats.get('backend', 'unknown'),
                'stats': cache_stats
            }
    except Exception as e:
        health_data['checks']['cache'] = {
            'status': 'error',
            'error': str(e)
        }
    
    return web.json_response(health_data)
