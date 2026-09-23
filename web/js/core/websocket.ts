/**
 * WebSocketManager — Real-time per-feature streaming with catch-up support.
 *
 * Protocol (client → server):
 *   {type: "auth", token_type: "bearer", token: "..."}
 *   {type: "auth", token_type: "telegram_init_data", init_data: "..."}
 *   {type: "get_events", since_timestamp: "<ISO>" | null, since_id: <int> | null, since_message_id: <int> | null}
 *   {type: "ping"}
 *
 * Protocol (server → client):
 *   {type: "auth_ok"}
 *   {type: "feature",        data: GeoJSON Feature}
 *   {type: "resync_required"}        — кэш клиента устарел: очистить store+localStorage
 *   {type: "events_snapshot_end", ...}
 *   {type: "events_cleaned", data: {...}}
 *   {type: "pong",           timestamp: "..."}
 */

import { EventFeature } from '../types/geojson';

export class WebSocketManager {
    private ws: WebSocket | null = null;
    public isConnected = false;

    private reconnectAttempts = 0;
    private readonly baseReconnectDelay = 1000;
    private readonly reconnectMultiplier = 1.5;
    private reconnectTimer: number | null = null;

    // Self-heal lifecycle: слушатели visibility/online/Telegram-activated
    // навешиваются один раз; intentionallyClosed гасит авто-reconnect после
    // явного disconnect() (logout) — чтобы resume-события его не оживляли.
    private lifecycleBound = false;
    private intentionallyClosed = false;

    // Heartbeat — ping every 25 s, expect pong within 15 s
    private readonly PING_INTERVAL_MS = 25_000;
    private readonly PONG_TIMEOUT_MS  = 15_000;
    private pingTimer:  number | null = null;
    private pongTimer:  number | null = null;
    private missedPongs = 0;
    private readonly maxMissedPongs = 2;

    /** Called once per live-pushed GeoJSON Feature (after the snapshot). */
    public onFeature: ((feature: EventFeature) => void) | null = null;

    /** Called once with the full batch when an event snapshot completes. */
    public onSnapshot: ((features: EventFeature[]) => void) | null = null;

    /** Called when connection status changes */
    public onConnectionStatusChange: ((connected: boolean) => void) | null = null;

    // Snapshot state — features between get_events and events_snapshot_end are
    // a batch sync (silent); features outside that window are live pushes.
    private receivingSnapshot = false;
    private snapshotBuffer: EventFeature[] = [];
    private snapshotTimer: number | null = null;
    private readonly SNAPSHOT_TIMEOUT_MS = 10_000;

    // Credential wait — polling sessionStorage until access_token appears.
    // Unlimited retries with exponential backoff (cap 30s).
    private credentialRetryRunning = false;
    private credentialRetryTimer: number | null = null;
    private credentialRetryCount = 0;

    // ------------------------------------------------------------------ connect

    /** Open WebSocket connection with bearer or Telegram auth. */
    connect(): void {
        this.bindLifecycle();
        this.intentionallyClosed = false;
        if (this.isConnected || this.ws?.readyState === WebSocket.CONNECTING) {
            console.log('[WS] Already connecting/connected, skipping');
            return;
        }

        const accessToken = sessionStorage.getItem('access_token');
        const initData    = window.Telegram?.WebApp?.initData;

        if (!accessToken && !initData) {
            if (!this.credentialRetryRunning) {
                console.warn('[WS] No auth credentials — starting credential wait loop');
                this.startCredentialWait();
            }
            return;
        }

        try {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const { hostname, port } = new URL(window.location.href);
            const wsUrl = `${protocol}//${hostname}${port ? ':' + port : ''}/ws`;

            console.log('[WS] Connecting to', wsUrl);
            this.ws = new WebSocket(wsUrl);

            this.ws.onopen    = () => this.handleOpen();
            this.ws.onmessage = (e) => this.handleMessage(e);
            this.ws.onclose   = (e) => this.handleClose(e);
            this.ws.onerror   = (e) => this.handleError(e);
        } catch (err) {
            console.error('[WS] Failed to create WebSocket:', err);
            this.scheduleReconnect();
        }
    }

    // --------------------------------------------------------------- handleOpen

    /** Authenticate and start heartbeat on successful connection. */
    private handleOpen(): void {
        console.log('[WS] Connected');
        this.isConnected      = true;
        this.reconnectAttempts = 0;
        this.missedPongs      = 0;

        // Reset credential wait — token is valid, connection succeeded
        this.credentialRetryRunning = false;
        if (this.credentialRetryTimer !== null) {
            window.clearTimeout(this.credentialRetryTimer);
            this.credentialRetryTimer = null;
        }
        this.credentialRetryCount = 0;

        this.sendAuth();
        this.startHeartbeat();
        this.onConnectionStatusChange?.(true);
        // get_events is sent after the server responds with auth_ok (see handleMessage)
    }

    /** Request events from server — called after auth_ok is received */
    private requestEvents(): void {
        const state = window.store?.getState?.();
        const since = state?.getLatestTimestamp?.() ?? null;
        // Catch-up watermark = max message_id: стабилен между рестартами БД
        // (события пере-вставляются с теми же Telegram message_id), тогда
        // как id события перезапускается — since_id не годится как
        // watermark (баг «biggе кэш vs сервер» после down -v).
        const sinceMessageId = state?.getLatestMessageId?.() ?? null;
        const sinceId = state?.getLatestId?.() ?? null;
        console.log('[WS] Requesting events since:', since ?? 'initial load',
            '| since_message_id:', sinceMessageId ?? 'initial',
            '| since_id:', sinceId ?? 'initial');

        // Enter snapshot mode: features until events_snapshot_end are a batch
        // sync and must not raise per-event notifications.
        this.receivingSnapshot = true;
        this.snapshotBuffer = [];
        if (this.snapshotTimer !== null) window.clearTimeout(this.snapshotTimer);
        this.snapshotTimer = window.setTimeout(() => {
            console.warn(
                `[WS] Snapshot timeout (${this.SNAPSHOT_TIMEOUT_MS}ms) — ` +
                `flushing ${this.snapshotBuffer.length} buffered event(s) ` +
                `without events_snapshot_end`
            );
            this.finishSnapshot();
        }, this.SNAPSHOT_TIMEOUT_MS);

        this.sendMessage({
            type: 'get_events',
            since_timestamp: since,
            since_id: sinceId,
            since_message_id: sinceMessageId
        });
    }

    /** Flush the buffered snapshot batch and leave snapshot mode. */
    private finishSnapshot(): void {
        if (this.snapshotTimer !== null) {
            window.clearTimeout(this.snapshotTimer);
            this.snapshotTimer = null;
        }
        if (!this.receivingSnapshot) return;
        this.receivingSnapshot = false;
        const batch = this.snapshotBuffer;
        this.snapshotBuffer = [];
        console.log('[WS] Snapshot complete:', batch.length, 'events (silent batch)');
        this.onSnapshot?.(batch);
    }

    // ------------------------------------------------------------- sendAuth

    /** Send bearer or Telegram init-data auth to the server. */
    private sendAuth(): void {
        // Priority 1: Use JWT access token (most common after successful gate.js auth)
        const accessToken = sessionStorage.getItem('access_token');
        if (accessToken) {
            console.log('[WS] Auth: Using JWT access token');
            this.sendMessage({ 
                type: 'auth', 
                token: accessToken 
            });
            return;
        }

        // Priority 2: Use saved initData from gate.js (fallback for direct WS connection)
        const savedInitData = sessionStorage.getItem('telegram_init_data');
        if (savedInitData) {
            console.log('[WS] Auth: Using saved initData from gate.js');
            this.sendMessage({ 
                type: 'auth', 
                init_data: savedInitData 
            });
            return;
        }

        // Priority 3: Try to get fresh initData from Telegram WebApp (last resort)
        const liveInitData = window.Telegram?.WebApp?.initData;
        if (liveInitData) {
            console.log('[WS] Auth: Using live Telegram WebApp initData');
            this.sendMessage({ 
                type: 'auth', 
                init_data: liveInitData 
            });
            return;
        }

        // No auth credentials available - this should not happen after gate.js
        console.error('[WS] No authentication credentials available');
        console.error('[WS] Debug info:', {
            hasAccessToken: !!accessToken,
            hasSavedInitData: !!savedInitData,
            hasLiveInitData: !!liveInitData,
            telegramWebApp: !!window.Telegram?.WebApp
        });
    }

    // ----------------------------------------------------------- handleMessage

    /** Parse incoming JSON and dispatch by message type. */
    private handleMessage(event: MessageEvent): void {
        let data: Record<string, unknown>;
        try {
            data = JSON.parse(event.data as string);
        } catch {
            console.error('[WS] Invalid JSON from server');
            return;
        }

        // Anchor the filtering clock to the server (Kiev) time — every server
        // envelope carries `timestamp`. This keeps time filtering correct even
        // when the device clock or timezone is wrong.
        if (typeof data.timestamp === 'string') {
            const serverMs = Date.parse(data.timestamp);
            if (!Number.isNaN(serverMs)) {
                window.serverClockOffsetMs = serverMs - Date.now();
            }
        }

        const type = data.type as string;

        switch (type) {
            case 'feature': {
                const feature = data.data as EventFeature;
                if (feature?.type === 'Feature') {
                    if (this.receivingSnapshot) {
                        // Batch sync — buffer silently, flush on snapshot end.
                        this.snapshotBuffer.push(feature);
                    } else {
                        // Live push — a genuinely new event.
                        this.onFeature?.(feature);
                    }
                }
                break;
            }

            case 'events_snapshot_end':
                this.finishSnapshot();
                break;

            case 'auth_ok':
                console.log('[WS] Auth acknowledged by server');
                this.requestEvents();
                break;

            case 'pong':
                this.handlePong();
                break;

            case 'events_cleaned':
                console.log('[WS] events_cleaned notification');
                window.store?.getState?.().pruneExpired?.();
                break;

            case 'resync_required':
                // Водяной знак клиента вне диапазона БД (рестарт БД/чистка
                // партиций) — кэш устарел или чужой. Очищаем store и
                // localStorage: следующие features — полный snapshot 60-мин
                // окна (requestEvents уже в режиме snapshot-буфера).
                console.warn('[WS] resync_required: cache out of sync — clearing store & local cache');
                window.store?.getState?.().clearEvents?.();
                if (typeof window.localCache?.invalidate === 'function') {
                    window.localCache.invalidate();
                }
                break;

            default:
                console.log('[WS] Unhandled message type:', type);
        }
    }

    // ------------------------------------------------------------- handleClose

    /** Handle connection close with auto-reconnect unless intentional. */
    private handleClose(event: CloseEvent): void {
        console.log('[WS] Closed:', event.code, event.reason);
        this.isConnected = false;
        this.stopHeartbeat();
        this.onConnectionStatusChange?.(false);

        if (event.code !== 1000) {
            this.scheduleReconnect();
        }
    }

    /** Log WebSocket errors to the console. */
    private handleError(error: Event): void {
        console.error('[WS] Error:', error);
    }

    // --------------------------------------------------------- scheduleReconnect

    /** Schedule reconnect with exponential backoff and jitter. */
    private scheduleReconnect(): void {
        if (this.reconnectTimer !== null) return;

        this.reconnectAttempts++;
        const base = Math.min(
            this.baseReconnectDelay * Math.pow(this.reconnectMultiplier, this.reconnectAttempts - 1),
            30_000
        );
        // Jitter ±20% — чтобы множество клиентов не ломились на reconnect синхронно.
        const delay = Math.round(base * (0.8 + Math.random() * 0.4));

        console.log(`[WS] Reconnect in ${delay}ms (attempt ${this.reconnectAttempts})`);
        this.reconnectTimer = window.setTimeout(() => {
            this.reconnectTimer = null;
            this.connect();
        }, delay);
    }

    // ------------------------------------------------------ credential wait

    /**
     * Unlimited polling: periodically check sessionStorage for access_token.
     * On discovery → reset counter and call connect(). Backoff: 1s → 2s → 4s → ... → 30s cap.
     */
    private startCredentialWait(): void {
        this.credentialRetryRunning = true;

        const poll = () => {
            if (this.intentionallyClosed) {
                this.credentialRetryRunning = false;
                return;
            }

            const token = sessionStorage.getItem('access_token');
            const initData = window.Telegram?.WebApp?.initData;

            if (token || initData) {
                console.log('[WS] Credentials available — connecting');
                this.credentialRetryRunning = false;
                this.credentialRetryCount = 0;
                this.connect();
                return;
            }

            this.credentialRetryCount++;
            const delay = Math.min(
                1000 * Math.pow(1.5, this.credentialRetryCount - 1),
                30_000
            );
            console.log(`[WS] Waiting for credentials (check ${this.credentialRetryCount}, next in ${delay}ms)`);
            this.credentialRetryTimer = window.setTimeout(poll, delay);
        };

        poll();
    }

    // ------------------------------------------------------- self-heal lifecycle

    /**
     * Немедленный reconnect по «событию пробуждения» (вкладка снова видима,
     * сеть вернулась, Telegram-приложение активировано). Сбрасывает бюджет
     * попыток и отменяет отложенный backoff — пользователь ждёт здесь и сейчас.
     */
    private reconnectNow(reason: string): void {
        if (this.intentionallyClosed) return;
        if (this.isConnected || this.ws?.readyState === WebSocket.CONNECTING) return;
        console.log(`[WS] Reconnect now (${reason})`);
        if (this.reconnectTimer !== null) {
            window.clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        this.reconnectAttempts = 0;
        this.credentialRetryCount = 0;
        this.connect();
    }

    /** Навесить слушатели жизненного цикла один раз (idempotent). */
    private bindLifecycle(): void {
        if (this.lifecycleBound) return;
        this.lifecycleBound = true;

        // Возврат во вкладку — мобильный WebView часто рвёт сокет в фоне.
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                this.reconnectNow('visibilitychange');
            }
        });

        // Сеть вернулась (Wi-Fi↔mobile, туннель и т.п.).
        window.addEventListener('online', () => this.reconnectNow('online'));
        window.addEventListener('offline', () => console.warn('[WS] Network offline'));

        // Telegram Mini App снова на переднем плане.
        const tg: any = window.Telegram?.WebApp;
        if (tg && typeof tg.onEvent === 'function') {
            tg.onEvent('activated', () => this.reconnectNow('tg:activated'));
        }
    }

    // --------------------------------------------------------------- heartbeat

    /**
     * Heartbeat strategy:
     *   - Send ping every PING_INTERVAL_MS.
     *   - After each ping, start a pong timeout of PONG_TIMEOUT_MS.
     *   - On pong: reset timeout, reset missedPongs counter.
     *   - If pong timeout fires: increment missedPongs, close the socket
     *     after maxMissedPongs consecutive misses (triggers reconnect).
     *   - The server also sends its own heartbeat frame (heartbeat=30 in aiohttp),
     *     so the connection is kept alive from both sides.
     */
    private startHeartbeat(): void {
        this.stopHeartbeat();

        this.pingTimer = window.setInterval(() => {
            if (!this.isConnected) return;

            console.log('[WS] → ping');
            this.sendMessage({ type: 'ping' });

            // Arm pong timeout
            this.pongTimer = window.setTimeout(() => {
                this.missedPongs++;
                console.warn(`[WS] Pong timeout (missed: ${this.missedPongs}/${this.maxMissedPongs})`);
                if (this.missedPongs >= this.maxMissedPongs) {
                    console.error('[WS] Too many missed pongs — forcing reconnect');
                    this.ws?.close(4000, 'heartbeat timeout');
                }
            }, this.PONG_TIMEOUT_MS);

        }, this.PING_INTERVAL_MS);
    }

    /** Reset heartbeat timers on server pong response. */
    private handlePong(): void {
        console.log('[WS] ← pong');
        if (this.pongTimer !== null) {
            window.clearTimeout(this.pongTimer);
            this.pongTimer = null;
        }
        this.missedPongs = 0;
    }

    /** Clear all heartbeat ping/pong timers. */
    private stopHeartbeat(): void {
        if (this.pingTimer !== null) {
            window.clearInterval(this.pingTimer);
            this.pingTimer = null;
        }
        if (this.pongTimer !== null) {
            window.clearTimeout(this.pongTimer);
            this.pongTimer = null;
        }
    }

    // ---------------------------------------------------------------- sendMessage

    /** Send a JSON message if the WebSocket is open. */
    sendMessage(message: Record<string, unknown>): void {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(message));
        } else {
            console.warn('[WS] Cannot send — not connected:', message.type);
        }
    }

    // ---------------------------------------------------------------- disconnect

    /** Gracefully close connection and prevent auto-reconnect. */
    disconnect(): void {
        this.intentionallyClosed = true;
        this.stopHeartbeat();
        if (this.reconnectTimer !== null) {
            window.clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        if (this.snapshotTimer !== null) {
            window.clearTimeout(this.snapshotTimer);
            this.snapshotTimer = null;
        }
        this.credentialRetryRunning = false;
        if (this.credentialRetryTimer !== null) {
            window.clearTimeout(this.credentialRetryTimer);
            this.credentialRetryTimer = null;
        }
        this.receivingSnapshot = false;
        this.snapshotBuffer = [];
        this.reconnectAttempts = Infinity; // prevent auto-reconnect
        this.ws?.close(1000, 'client disconnect');
        this.ws = null;
        this.isConnected = false;
    }

    /** Return current connection diagnostics. */
    getStats() {
        return {
            isConnected: this.isConnected,
            reconnectAttempts: this.reconnectAttempts,
            missedPongs: this.missedPongs
        };
    }
}

// ------------------------------------------------------------------- singleton

window.webSocketManager = new WebSocketManager();

/** Fire UI notification for a newly received live event. */
function notifyNewEvent(feature: EventFeature): void {
    if (typeof window.handleNewEvents !== 'function') return;
    const p: any = feature.properties || {};
    window.handleNewEvents([{
        id: p.id,
        layer: p.layer || p.type || 'unknown',
        description: p.description || p.name || 'Новое событие'
    }]);
}

/** Wire WebSocketManager callbacks to the store and start connection. */
function initializeWebSocket(): void {
    console.log('[WS] Initializing...');

    // Live push — append to the store; notify only if the event is new.
    window.webSocketManager.onFeature = (feature: EventFeature) => {
        const isNew = window.store.getState().addEvent(feature);
        if (isNew) {
            notifyNewEvent(feature);
        }
    };

    // Snapshot batch (initial load or reconnect catch-up) — append silently.
    window.webSocketManager.onSnapshot = (features: EventFeature[]) => {
        window.store.getState().addEvents(features);
    };

    window.webSocketManager.onConnectionStatusChange = (connected: boolean) => {
        if (typeof window.updateOnlineStatus === 'function') {
            window.updateOnlineStatus(connected);
        }
        const indicator = document.getElementById('connection-indicator');
        if (indicator) {
            indicator.className = connected ? 'status-online' : 'status-offline';
            indicator.textContent = connected ? '' : 'Переподключение...';
        }
        console.log(connected ? '[WS] ✅ Live' : '[WS] ⚠️  Offline — serving localStorage');
    };

    window.webSocketManager.connect();
}

window.initializeWebSocket = initializeWebSocket;

console.log('✅ WebSocketManager initialized (per-feature protocol, reliable heartbeat)');
