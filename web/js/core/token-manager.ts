/**
 * JWT Token Manager - Token validation and automatic refresh
 *
 * Features:
 * - Check token expiration
 * - Auto-refresh tokens before expiration
 * - Store tokens in sessionStorage
 * - Handle refresh errors
 */

(function() {
    'use strict';

    // Private state
    let _refreshTimer: ReturnType<typeof setTimeout> | null = null;
    const _REFRESH_THRESHOLD_MS = 60000; // Refresh 1 minute before expiration

    /**
     * Decode JWT token payload (without verification)
     */
    function decodeToken(token: string): Record<string, unknown> | null {
        try {
            const base64Url = token.split('.')[1];
            if (!base64Url) return null;

            const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
            const jsonPayload = decodeURIComponent(
                atob(base64).split('').map(c =>
                    '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2)
                ).join('')
            );

            return JSON.parse(jsonPayload);
        } catch (e) {
            console.error('[TokenManager] Failed to decode token:', e);
            return null;
        }
    }

    /**
     * Check if token is expired
     */
    function isTokenExpired(token: string, thresholdMs: number = 0): boolean {
        const payload = decodeToken(token) as { exp?: number } | null;
        if (!payload || !payload.exp) return true;

        const expirationTime = payload.exp * 1000;
        const now = Date.now();

        return now >= (expirationTime - thresholdMs);
    }

    /**
     * Get token expiration time
     */
    function getTokenExpiration(token: string): Date | null {
        const payload = decodeToken(token) as { exp?: number } | null;
        if (!payload || !payload.exp) return null;

        return new Date(payload.exp * 1000);
    }

    /**
     * Get access token from sessionStorage
     */
    function getAccessToken() {
        return sessionStorage.getItem('access_token');
    }

    /**
     * Get refresh token from sessionStorage
     */
    function getRefreshToken() {
        return sessionStorage.getItem('refresh_token');
    }

    /**
     * Store tokens in sessionStorage
     */
    function storeTokens(accessToken: string, refreshToken?: string): void {
        sessionStorage.setItem('access_token', accessToken);
        if (refreshToken) {
            sessionStorage.setItem('refresh_token', refreshToken);
        }
    }

    /**
     * Clear tokens from sessionStorage
     */
    function clearTokens() {
        sessionStorage.removeItem('access_token');
        sessionStorage.removeItem('refresh_token');
        sessionStorage.removeItem('user');
    }

    /**
     * Refresh access token using refresh token
     */
    async function refreshAccessToken(): Promise<string | null> {
        const refreshToken = getRefreshToken();

        if (!refreshToken) {
            console.warn('[TokenManager] No refresh token available');
            return null;
        }

        try {
            console.log('[TokenManager] Refreshing access token...');

            const response = await fetch('/api/auth/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh_token: refreshToken })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Refresh failed');
            }

            const result = await response.json();

            if (result.access_token) {
                console.log('[TokenManager] Token refreshed successfully');
                // Сохраняем новый refresh_token от сервера (Rotation: старый уже инвалидирован)
                storeTokens(result.access_token, result.refresh_token || refreshToken);
                return result.access_token;
            }

            return null;

        } catch (error) {
            console.error('[TokenManager] Token refresh failed:', error);
            clearTokens();
            return null;
        }
    }

    /**
     * Schedule token refresh
     */
    function scheduleRefresh() {
        const accessToken = getAccessToken();

        if (!accessToken) {
            console.log('[TokenManager] No access token, skipping refresh schedule');
            return;
        }

        const payload = decodeToken(accessToken) as { exp?: number } | null;
        if (!payload || !payload.exp) {
            console.warn('[TokenManager] Invalid token, cannot schedule refresh');
            return;
        }

        const expirationTime = payload.exp * 1000;
        const now = Date.now();
        const timeUntilExpiration = expirationTime - now;
        const refreshTime = Math.max(0, timeUntilExpiration - _REFRESH_THRESHOLD_MS);

        console.log(`[TokenManager] Token expires in ${Math.round(timeUntilExpiration / 1000)}s, refreshing in ${Math.round(refreshTime / 1000)}s`);

        // Clear existing timer
        if (_refreshTimer) {
            clearTimeout(_refreshTimer);
        }

        // Schedule refresh
        _refreshTimer = setTimeout(async () => {
            console.log('[TokenManager] Auto-refreshing token...');
            const newToken = await refreshAccessToken();

            if (newToken) {
                // Schedule next refresh
                scheduleRefresh();
            } else {
                console.warn('[TokenManager] Token refresh failed, user may need to re-authenticate');
            }
        }, refreshTime);
    }

    /**
     * Initialize token manager
     */
    async function init() {
        const accessToken = getAccessToken();

        if (!accessToken) {
            // No access_token — try refresh if refresh_token exists
            const rt = getRefreshToken();
            if (rt) {
                console.log('[TokenManager] No access token, attempting refresh...');
                const refreshed = await refreshAccessToken();
                if (refreshed) {
                    scheduleRefresh();
                    console.log('[TokenManager] Initialized via refresh');
                    return true;
                }
            }
            console.log('[TokenManager] No access token found');
            return false;
        }

        // Check if token is already expired
        if (isTokenExpired(accessToken)) {
            console.log('[TokenManager] Access token expired, attempting refresh...');
            const newToken = await refreshAccessToken();

            if (!newToken) {
                console.warn('[TokenManager] Failed to refresh expired token');
                return false;
            }
        }

        // Schedule automatic refresh
        scheduleRefresh();

        console.log('[TokenManager] Initialized successfully');
        return true;
    }

    /**
     * Get valid access token (refresh if needed)
     */
    async function getValidToken() {
        let accessToken = getAccessToken();

        if (!accessToken) {
            console.warn('[TokenManager] No access token available');
            return null;
        }

        // Check if token is expired or about to expire
        if (isTokenExpired(accessToken, _REFRESH_THRESHOLD_MS)) {
            console.log('[TokenManager] Token expired or near expiration, refreshing...');
            accessToken = await refreshAccessToken();
        }

        return accessToken;
    }

    /**
     * Re-acquire a valid token:
     * 1. Check sessionStorage (token may have appeared from gate.js retry)
     * 2. Try refresh if refresh_token exists
     * Called by map-bootstrap.js poller and websocket credential wait.
     */
    async function acquireToken(): Promise<string | null> {
        const token = getAccessToken();
        if (token && !isTokenExpired(token)) return token;

        // Refresh if token exists but expired
        if (token && isTokenExpired(token)) {
            const refreshed = await refreshAccessToken();
            if (refreshed) return refreshed;
        }

        // Refresh if refresh_token exists but access_token is missing
        const rt = getRefreshToken();
        if (rt) {
            const refreshed = await refreshAccessToken();
            if (refreshed) return refreshed;
        }

        return null;
    }

    /**
     * Stop auto-refresh
     */
    function destroy() {
        if (_refreshTimer) {
            clearTimeout(_refreshTimer);
            _refreshTimer = null;
        }
        console.log('[TokenManager] Destroyed');
    }

    // Export public API
    window.tokenManager = {
        init,
        getAccessToken,
        getRefreshToken,
        getValidToken,
        acquireToken,
        storeTokens,
        clearTokens,
        isTokenExpired,
        getTokenExpiration,
        refreshAccessToken,
        scheduleRefresh,
        destroy
    };

    console.log('[TokenManager] Module loaded');
})();
