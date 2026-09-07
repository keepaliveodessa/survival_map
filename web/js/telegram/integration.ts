/**
 * Telegram Mini Apps Integration Module
 * Based on: https://core.telegram.org/bots/webapps
 * Implements all major features of Telegram Mini Apps API
 */

type TgWebApp = NonNullable<Window['Telegram']['WebApp']>;

interface CallbackMap {
    onActivated?: () => void;
    onDeactivated?: () => void;
    onViewportChanged?: (event: unknown) => void;
    [key: string]: ((...args: unknown[]) => void) | undefined;
}

class TelegramIntegration {
    private tg: TgWebApp | undefined;
    private isInitialized = false;
    private callbacks: CallbackMap = {};

    constructor() {
        this.tg = window.Telegram?.WebApp as TgWebApp | undefined;
    }

    /**
     * Initialize Telegram Mini App
     * Implements Bot API 8.0+ features
     */
    init(): boolean {
        if (!this.tg) {
            console.warn('Telegram WebApp SDK not available');
            return false;
        }

        // Bot API 6.1+: Готовность приложения
        this.tg.ready();

        // Bot API 6.1+: Разворачиваем на весь экран
        this.tg.expand();

        // Bot API 6.2+: Включаем подтверждение закрытия при необходимости
        try {
            if (this.tg.disableClosingConfirmation && this.tg.isVersionAtLeast?.('6.2')) {
                this.tg.disableClosingConfirmation();
            }
        } catch (_e) { /* ignore */ }

        // Bot API 7.7+: Включаем вертикальные свайпы для лучшего UX
        if (this.tg.isVersionAtLeast?.('7.7')) {
            this.tg.enableVerticalSwipes?.();
        }

        // Применяем тему Telegram
        this.applyTheme();

        // Слушаем изменения темы (Bot API 6.1+)
        this.tg.onEvent('themeChanged', () => {
            console.log('Telegram theme changed');
            this.applyTheme();
        });

        // Bot API 6.9+: Устанавливаем цвет header bar
        try {
            if (this.tg.setHeaderColor && this.tg.isVersionAtLeast?.('6.9')) {
                this.tg.setHeaderColor('secondary_bg_color');
            }
        } catch (_e) { /* ignore */ }

        // Bot API 7.10+: Устанавливаем цвет bottom bar
        try {
            if (this.tg.setBottomBarColor && this.tg.isVersionAtLeast?.('7.10')) {
                this.tg.setBottomBarColor('secondary_bg_color');
            }
        } catch (_e) { /* ignore */ }

        // VIEWPORT И SAFE AREA (Bot API 8.0+)
        if (this.tg.safeAreaInset) {
            this.applySafeArea();
            this.tg.onEvent('safeAreaChanged', () => { this.applySafeArea(); });
        }

        if (this.tg.contentSafeAreaInset) {
            this.applyContentSafeArea();
            this.tg.onEvent('contentSafeAreaChanged', () => { this.applyContentSafeArea(); });
        }

        // LIFECYCLE EVENTS (Bot API 8.0+)
        if (this.tg.isVersionAtLeast?.('8.0')) {
            this.tg.onEvent('activated', () => {
                console.log('Mini App activated');
                this.callbacks.onActivated?.();
            });
            this.tg.onEvent('deactivated', () => {
                console.log('Mini App deactivated');
                this.callbacks.onDeactivated?.();
            });
        }

        // VIEWPORT CHANGES
        this.tg.onEvent('viewportChanged', (event: unknown) => {
            console.log('Viewport changed:', event);
            this.callbacks.onViewportChanged?.(event);
        });

        this.isInitialized = true;

        if (window.location.hostname === 'localhost') {
            console.log('✅ Telegram Mini App initialized', {
                version: this.tg.version,
                platform: this.tg.platform,
                colorScheme: this.tg.colorScheme
            });
        }

        return true;
    }

    /**
     * Применяет тему Telegram к приложению
     */
    applyTheme(): void {
        if (!this.tg) return;
        const { themeParams, colorScheme } = this.tg;
        const root = document.documentElement;

        const themeMap: [string, string | undefined][] = [
            ['--tg-bg-color', themeParams.bg_color],
            ['--tg-text-color', themeParams.text_color],
            ['--tg-hint-color', themeParams.hint_color],
            ['--tg-link-color', themeParams.link_color],
            ['--tg-button-color', themeParams.button_color],
            ['--tg-button-text-color', themeParams.button_text_color],
            ['--tg-secondary-bg-color', themeParams.secondary_bg_color],
            ['--tg-header-bg-color', themeParams.header_bg_color],
            ['--tg-accent-text-color', themeParams.accent_text_color],
            ['--tg-section-bg-color', themeParams.section_bg_color],
            ['--tg-section-header-text-color', themeParams.section_header_text_color],
            ['--tg-subtitle-text-color', themeParams.subtitle_text_color],
            ['--tg-destructive-text-color', themeParams.destructive_text_color],
            ['--tg-section-separator-color', themeParams.section_separator_color],
            ['--tg-bottom-bar-bg-color', themeParams.bottom_bar_bg_color],
        ];

        for (const [prop, value] of themeMap) {
            if (value) root.style.setProperty(prop, value);
        }

        document.body.classList.toggle('dark-theme', colorScheme === 'dark');
        document.body.classList.toggle('light-theme', colorScheme === 'light');

        const metaThemeColor = document.querySelector('meta[name="theme-color"]');
        if (metaThemeColor && themeParams.bg_color) {
            metaThemeColor.setAttribute('content', themeParams.bg_color);
        }

        console.log('🎨 Telegram theme applied:', colorScheme);
    }

    applySafeArea(): void {
        const inset = this.tg?.safeAreaInset;
        if (!inset) return;
        const root = document.documentElement;
        root.style.setProperty('--tg-safe-area-inset-top', `${inset.top}px`);
        root.style.setProperty('--tg-safe-area-inset-right', `${inset.right}px`);
        root.style.setProperty('--tg-safe-area-inset-bottom', `${inset.bottom}px`);
        root.style.setProperty('--tg-safe-area-inset-left', `${inset.left}px`);
    }

    applyContentSafeArea(): void {
        const inset = this.tg?.contentSafeAreaInset;
        if (!inset) return;
        const root = document.documentElement;
        root.style.setProperty('--tg-content-safe-area-inset-top', `${inset.top}px`);
        root.style.setProperty('--tg-content-safe-area-inset-right', `${inset.right}px`);
        root.style.setProperty('--tg-content-safe-area-inset-bottom', `${inset.bottom}px`);
        root.style.setProperty('--tg-content-safe-area-inset-left', `${inset.left}px`);
    }

    hapticFeedback(type: string = 'medium'): boolean {
        if (!this.tg?.HapticFeedback) return false;
        const versionOk = !!(this.tg?.isVersionAtLeast?.('6.1'));
        if (!versionOk) return false;

        try {
            switch (type) {
                case 'light':
                case 'medium':
                case 'heavy':
                    this.tg.HapticFeedback.impactOccurred(type as 'light' | 'medium' | 'heavy');
                    return true;
                case 'success':
                case 'warning':
                case 'error':
                    this.tg.HapticFeedback.notificationOccurred(type as 'success' | 'warning' | 'error');
                    return true;
                case 'selection_changed':
                    this.tg.HapticFeedback.selectionChanged();
                    return true;
                default:
                    this.tg.HapticFeedback.impactOccurred('medium');
                    return true;
            }
        } catch (e) {
            console.warn('Haptic feedback failed:', e);
            return false;
        }
    }

    /**
     * Fire haptic feedback with the full fallback chain (Rule 5):
     * window.hapticFeedback (common.js) tries native HapticFeedback →
     * this wrapper → HTML5 Vibration API. Falls back to the native-only
     * wrapper if common.js is not loaded yet.
     */
    private fireHaptic(type: string): void {
        if (typeof window.hapticFeedback === 'function') {
            try {
                window.hapticFeedback(type);
                return;
            } catch (_e) { /* fall through to native-only */ }
        }
        this.hapticFeedback(type);
    }

    showPopup(message: string, buttons: Array<{ type: string }> = [{ type: 'ok' }], haptic: string = 'light'): Promise<string> {
        // Rule 5: every popup notification is accompanied by haptic feedback.
        this.fireHaptic(haptic);
        const minOk = !!(this.tg?.isVersionAtLeast?.('6.2'));
        if (!this.tg?.showPopup || !minOk) {
            alert(message);
            return Promise.resolve('');
        }

        return new Promise((resolve) => {
            try {
                this.tg!.showPopup!({ message, buttons }, (buttonId: string) => {
                    resolve(buttonId);
                });
            } catch (_e) {
                alert(message);
                resolve('');
            }
        });
    }

    showAlert(message: string, haptic: string = 'light'): Promise<void> {
        // Rule 5: every alert notification is accompanied by haptic feedback.
        this.fireHaptic(haptic);
        const minOk = !!(this.tg?.isVersionAtLeast?.('6.2'));
        if (!this.tg?.showAlert || !minOk) {
            alert(message);
            return Promise.resolve();
        }

        return new Promise((resolve) => {
            try {
                this.tg!.showAlert!(message, () => { resolve(); });
            } catch (_e) {
                alert(message);
                resolve();
            }
        });
    }

    showConfirm(message: string, haptic: string = 'light'): Promise<boolean> {
        // Rule 5: confirm dialogs are notifications too.
        this.fireHaptic(haptic);
        if (!this.tg?.showConfirm) {
            return Promise.resolve(confirm(message));
        }

        return new Promise((resolve) => {                this.tg!.showConfirm!(message, (result: boolean): void => { resolve(result); });
        });
    }

    setClosingConfirmation(enabled: boolean): void {
        if (!this.tg) return;
        if (enabled) {
            this.tg.enableClosingConfirmation?.();
        } else {
            this.tg.disableClosingConfirmation?.();
        }
    }

    async readTextFromClipboard(): Promise<string | null> {
        if (!this.tg?.readTextFromClipboard) {
            try {
                return await navigator.clipboard.readText();
            } catch (_e) {
                return null;
            }
        }

        return new Promise((resolve) => {
            this.tg!.readTextFromClipboard!((text: string | null) => {
                resolve(text || null);
            });
        });
    }

    switchInlineQuery(query: string, chatTypes: string[] = ['users', 'bots', 'groups', 'channels']): void {
        if (!this.tg?.switchInlineQuery) {
            console.warn('switchInlineQuery not supported');
            return;
        }
        this.tg.switchInlineQuery(query, chatTypes);
    }

    async cloudStorageSet(key: string, value: string): Promise<boolean> {
        if (!this.tg?.CloudStorage) {
            try {
                localStorage.setItem(key, value);
                return true;
            } catch (_e) {
                return false;
            }
        }

        return new Promise((resolve) => {
            this.tg!.CloudStorage!.setItem(key, value, (error: string | null, success?: boolean) => {
                if (error) {
                    console.error('CloudStorage set error:', error);
                    resolve(false);
                } else {
                    resolve(!!success);
                }
            });
        });
    }

    async cloudStorageGet(key: string): Promise<string | null> {
        if (!this.tg?.CloudStorage) {
            try {
                return localStorage.getItem(key);
            } catch (_e) {
                return null;
            }
        }

        return new Promise((resolve) => {
            this.tg!.CloudStorage!.getItem(key, (error: string | null, value: string | undefined) => {
                if (error) {
                    console.error('CloudStorage get error:', error);
                    resolve(null);
                } else {
                    resolve(value || null);
                }
            });
        });
    }

    async shareToStory(mediaUrl: string, options: Record<string, unknown> = {}): Promise<boolean> {
        if (!this.tg?.shareToStory) {
            console.warn('shareToStory not supported');
            return false;
        }
        try {
            this.tg.shareToStory(mediaUrl, options);
            return true;
        } catch (e) {
            console.error('Share to story failed:', e);
            return false;
        }
    }

    async shareMessage(text: string, url: string | null = null): Promise<boolean> {
        if (!this.tg?.shareMessage) {
            if (navigator.share) {
                try {
                    await navigator.share({ text, url: url ?? undefined });
                    return true;
                } catch (_e) {
                    return false;
                }
            }
            console.warn('shareMessage not supported');
            return false;
        }

        return new Promise((resolve) => {
            this.tg!.shareMessage!(text, url, (success: boolean) => {
                resolve(success);
            });
        });
    }

    async downloadFile(url: string, filename: string): Promise<boolean> {
        if (!this.tg?.downloadFile) {
            try {
                const a = document.createElement('a');
                a.href = url;
                a.download = filename;
                a.click();
                return true;
            } catch (e) {
                console.error('Download failed:', e);
                return false;
            }
        }

        return new Promise((resolve) => {
            this.tg!.downloadFile!({ url, file_name: filename }, (result: { status?: string } | null) => {
                resolve(result?.status === 'downloading');
            });
        });
    }

    async addToHomeScreen(): Promise<boolean> {
        if (!this.tg?.addToHomeScreen) {
            console.warn('addToHomeScreen not supported');
            return false;
        }

        return new Promise((resolve) => {
            this.tg!.addToHomeScreen!();
            const handler = (): void => {
                this.tg!.offEvent!('homeScreenAdded', handler);
                resolve(true);
            };
            this.tg!.onEvent!('homeScreenAdded', handler);
            setTimeout(() => {
                this.tg!.offEvent!('homeScreenAdded', handler);
                resolve(false);
            }, 5000);
        });
    }

    async checkHomeScreenStatus(): Promise<string> {
        if (!this.tg?.checkHomeScreenStatus) {
            return 'unsupported';
        }

        return new Promise((resolve) => {
            this.tg!.checkHomeScreenStatus!((status: string) => {
                resolve(status);
            });
        });
    }

    requestFullscreen(): boolean {
        if (!this.tg?.requestFullscreen) {
            if (document.documentElement.requestFullscreen) {
                document.documentElement.requestFullscreen();
                return true;
            }
            return false;
        }
        this.tg.requestFullscreen();
        return true;
    }

    exitFullscreen(): boolean {
        if (!this.tg?.exitFullscreen) {
            if (document.exitFullscreen && document.fullscreenElement) {
                document.exitFullscreen();
                return true;
            }
            return false;
        }
        this.tg.exitFullscreen();
        return true;
    }

    lockOrientation(orientation: string = 'portrait'): void {
        if (!this.tg?.lockOrientation) {
            const so = screen.orientation as ScreenOrientation & { lock?: (o: string) => Promise<void> };
            if (so.lock) {
                so.lock(orientation).catch((_e: unknown) => { // eslint-disable-line @typescript-eslint/no-unused-vars
                    console.warn('Orientation lock failed');
                });
            }
            return;
        }
        this.tg.lockOrientation();
    }

    unlockOrientation(): void {
        if (!this.tg?.unlockOrientation) {
            screen.orientation?.unlock();
            return;
        }
        this.tg.unlockOrientation();
    }

    async requestLocation(): Promise<{ latitude: number; longitude: number; altitude?: number; accuracy?: number }> {
        const lm = this.tg?.LocationManager;

        // Библиотека geo-доступа есть только в Bot API 8.0+. На старых клиентах
        // или если библиотека недоступна — фолбэк на браузерный geolocation.
        if (!lm) {
            return this._requestBrowserGeolocation();
        }

        try {
            // LocationManager.init() обязателен перед getLocation(): без него
            // официальный telegram-web-app.js бросает WebAppLocationManagerNotInited.
            // init() = postEvent('web_app_check_location') — опрашивает устройство
            // (доступно ли гео, запрашивал ли доступ).
            if (!lm.isInited) {
                await new Promise<void>((resolveInit) => {
                    lm.init!(() => resolveInit());
                    // Таймаут-страховка: если событие location_checked не придёт
                    // (клиент без поддержки / оффлайн), не зависаем навсегда.
                    setTimeout(() => resolveInit(), 5000);
                });
            }

            // Гео недоступно на устройстве / доступ не выдан → фолбэк на браузер.
            if (!lm.isLocationAvailable || !lm.isAccessGranted) {
                return this._requestBrowserGeolocation();
            }

            return await new Promise((resolve, reject) => {
                lm.getLocation!((location: { latitude: number; longitude: number; altitude?: number; horizontal_accuracy?: number } | null) => {
                    if (location) {
                        resolve({
                            latitude: location.latitude,
                            longitude: location.longitude,
                            altitude: location.altitude,
                            // telegram-web-app.js отдаёт horizontal_accuracy,
                            // navigator.geolocation — accuracy.
                            accuracy: location.horizontal_accuracy
                        });
                    } else {
                        reject(new Error('Location denied'));
                    }
                });
            });
        } catch (e) {
            // WebAppLocationManagerNotInited / LocationNotAvailable и прочие
            // ошибки библиотеки — фолбэк на браузерный geolocation.
            console.warn('[TG] LocationManager failed, falling back to browser geolocation:', e);
            return this._requestBrowserGeolocation();
        }
    }

    /** Браузерный geolocation (фолбэк, когда Telegram-библиотека недоступна). */
    private _requestBrowserGeolocation(): Promise<{ latitude: number; longitude: number; altitude?: number; accuracy?: number }> {
        if (!navigator.geolocation) {
            return Promise.reject(new Error('Geolocation not supported'));
        }
        return new Promise((resolve, reject) => {
            navigator.geolocation.getCurrentPosition(
                (position) => {
                    resolve({
                        latitude: position.coords.latitude,
                        longitude: position.coords.longitude,
                        altitude: position.coords.altitude ?? undefined,
                        accuracy: position.coords.accuracy
                    });
                },
                (error) => { reject(error); }
            );
        });
    }

    openTelegramLink(url: string): void {
        if (!this.tg?.openTelegramLink) {
            window.open(url, '_blank');
            return;
        }
        this.tg.openTelegramLink(url);
    }

    openLink(url: string, options: Record<string, unknown> = {}): void {
        if (!this.tg?.openLink) {
            window.open(url, '_blank');
            return;
        }
        this.tg.openLink(url, options);
    }

    close(): void {
        if (!this.tg?.close) {
            window.close();
            return;
        }
        this.tg.close();
    }

    on(event: string, callback: (...args: unknown[]) => void): void {
        this.callbacks[event] = callback;
    }

    getPlatformInfo(): Record<string, unknown> {
        if (!this.tg) return {};

        return {
            platform: this.tg.platform,
            version: this.tg.version,
            colorScheme: this.tg.colorScheme,
            isExpanded: this.tg.isExpanded,
            viewportHeight: this.tg.viewportHeight,
            viewportStableHeight: this.tg.viewportStableHeight,
            isFullscreen: this.tg.isFullscreen,
            isActive: this.tg.isActive
        };
    }
}

// Export singleton instance
window.telegramIntegration = new TelegramIntegration();
