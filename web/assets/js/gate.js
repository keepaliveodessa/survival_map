// Validation gate (was inline in index.html — externalized for strict CSP).
(async function() {
    const statusEl = document.getElementById('status');

    let redirectUrl = 'https://github.com/404';

    function redirectTo(url) {
        console.log('[Gate] Redirecting to:', url);
        window.location.replace(url);
    }

    function logRedirect(reason) {
        fetch('/api/gate-redirect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                reason: reason,
                user_agent: navigator.userAgent
            })
        }).catch(() => {});
    }

    function isAbsoluteUrl(url) {
        try {
            new URL(url);
            return true;
        } catch {
            return false;
        }
    }

    async function loadConfig() {
        let attempt = 0;
        while (true) {
            attempt++;
            try {
                statusEl.textContent = attempt === 1
                    ? 'Загрузка конфигурации...'
                    : `Подключение к серверу (попытка ${attempt})...`;
                const response = await fetch('/api/validation-config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({})
                });
                if (!response.ok) throw new Error('Config load failed');
                return await response.json();
            } catch (e) {
                console.warn(`[Gate] Config load attempt ${attempt} failed:`, e.message);
                const delay = Math.min(1000 * Math.pow(2, attempt - 1), 30_000);
                await new Promise(r => setTimeout(r, delay));
            }
        }
    }

    async function validateAndAuth(initData) {
        let attempt = 0;
        while (true) {
            attempt++;
            try {
                statusEl.textContent = attempt === 1
                    ? 'Вход в систему...'
                    : `Вход в систему (попытка ${attempt})...`;
                const response = await fetch('/api/validate-init', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ init_data: initData })
                });
                const result = await response.json();

                if (result.valid && result.access_token) {
                    sessionStorage.setItem('access_token', result.access_token);
                    sessionStorage.setItem('refresh_token', result.refresh_token);
                    sessionStorage.setItem('user', JSON.stringify(result.user));
                    return true;
                }
            } catch (e) {
                console.warn(`[Gate] Auth attempt ${attempt} failed:`, e.message);
            }
            const delay = Math.min(1000 * Math.pow(2, attempt - 1), 30_000);
            await new Promise(r => setTimeout(r, delay));
        }
    }

    try {
        // Load configuration
        statusEl.textContent = 'Загрузка конфигурации...';
        const config = await loadConfig();

        // Set redirect URL (fallback to GitHub 404)
        redirectUrl = config.redirect_url || 'https://github.com/404';
        if (!isAbsoluteUrl(redirectUrl)) {
            console.error('[Gate] Invalid redirect_url (bare domain causes redirect loop):', redirectUrl);
            redirectUrl = 'https://github.com/404';
        }

        console.log('[Gate] Config loaded:', {
            validationEnabled: config.telegram_webview_validation,
            redirectUrl: redirectUrl
        });

        // Check if validation is disabled (dev mode).
        // === false: отсутствующий ключ (старый core) трактуется как strict —
        // Secure by Default, согласуется с _parse_strict_bool на бэкенде.
        if (config.telegram_webview_validation === false) {
            console.log('[Gate] Validation disabled (development mode)');
            sessionStorage.setItem('dev_mode', 'true');

            // Retry until we get a token — backend may be starting up
            let attempt = 0;
            while (true) {
                attempt++;
                try {
                    statusEl.textContent = attempt === 1
                        ? 'Режим разработки...'
                        : `Получение токена (попытка ${attempt})...`;
                    const response = await fetch('/api/validate-init', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ init_data: '' })
                    });
                    const result = await response.json();
                    if (result.valid && result.access_token) {
                        sessionStorage.setItem('access_token', result.access_token);
                        sessionStorage.setItem('refresh_token', result.refresh_token);
                        sessionStorage.setItem('user', JSON.stringify(result.user));
                        console.log('[Gate] Dev token acquired on attempt', attempt);
                        break;
                    }
                } catch (e) {
                    console.warn(`[Gate] Dev token attempt ${attempt} failed:`, e.message);
                }
                const delay = Math.min(1000 * Math.pow(2, attempt - 1), 30_000);
                await new Promise(r => setTimeout(r, delay));
            }

            setTimeout(() => redirectTo('/map.html'), 300);
            return;
        }

        // Validate existing session — if a token exists in sessionStorage,
        // confirm it's still valid before skipping re-auth. After a server
        // restart (JWT_SECRET change) old tokens become invalid and must be
        // cleared to avoid an infinite redirect loop between gate ↔ map.
        const existingToken = sessionStorage.getItem('access_token');
        if (existingToken) {
            try {
                const checkResp = await fetch('/api/config', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + existingToken
                    },
                    body: JSON.stringify({})
                });
                if (checkResp.ok) {
                    // Token valid — skip re-auth, go straight to map
                    console.log('[Gate] Existing session valid, skipping re-auth');
                    setTimeout(() => redirectTo('/map.html'), 100);
                    return;
                }
                // Token invalid — clear stale session
                console.warn('[Gate] Existing session invalid (status=' + checkResp.status + '), clearing');
                sessionStorage.removeItem('access_token');
                sessionStorage.removeItem('refresh_token');
                sessionStorage.removeItem('user');
                sessionStorage.removeItem('dev_mode');
            } catch (e) {
                // Network error — clear and re-validate via Telegram
                console.warn('[Gate] Session check failed, clearing:', e);
                sessionStorage.removeItem('access_token');
                sessionStorage.removeItem('refresh_token');
                sessionStorage.removeItem('user');
                sessionStorage.removeItem('dev_mode');
            }
        }

        // Check Telegram WebApp
        if (!window.Telegram || !window.Telegram.WebApp) {
            console.warn('[Gate] Not Telegram WebApp');
            statusEl.textContent = 'Перенаправление...';
            logRedirect('sdk_unavailable');
            setTimeout(() => redirectTo(redirectUrl), 100);
            return;
        }

        const tg = window.Telegram.WebApp;
        const initData = tg.initData;

        if (!initData) {
            console.warn('[Gate] No initData');
            statusEl.textContent = 'Перенаправление...';
            logRedirect('no_initData');
            setTimeout(() => redirectTo(redirectUrl), 100);
            return;
        }

        // Validate and get tokens
        statusEl.textContent = 'Вход в систему...';
        const isValid = await validateAndAuth(initData);

        if (!isValid) {
            console.warn('[Gate] Validation failed');
            statusEl.textContent = 'Перенаправление...';
            logRedirect('validation_failed');
            setTimeout(() => redirectTo(redirectUrl), 100);
            return;
        }

        // Success - redirect to map
        console.log('[Gate] Validation successful');
        statusEl.textContent = 'Готово!';
        tg.ready();
        tg.expand();
        setTimeout(() => redirectTo('/map.html'), 300);

    } catch (e) {
        console.error('[Gate] Error:', e);
        statusEl.textContent = 'Перенаправление...';
        logRedirect('gate_error');
        setTimeout(() => redirectTo(redirectUrl), 100);
    }
})();
