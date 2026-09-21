// map.js — функции создания геометрии на карте
// Архитектурно оптимизированная версия для Telegram Mini Apps

// Глобальные функции для создания геометрии
// Эти функции будут доступны глобально после загрузки скрипта

const ICON_CONFIG: Record<string, { url: string; size: [number, number] }> = {
    bus: { url: '/assets/images/bus.webp', size: [25, 25] },
    pig: { url: '/assets/images/pig.webp', size: [25, 25] },
    cops: { url: '/assets/images/cops.webp', size: [25, 25] }
};

// Слой traffic не имеет PNG-иконки — рендерим эмодзи ⛔ через L.divIcon,
// согласовано с легендой и чекбоксом в #layerControls.
window.ICON_CONFIG = ICON_CONFIG;

window.preloadIcons = function(urls: string[]): void {
    for (const url of urls) {
        const img = new Image();
        img.src = url;
    }
};

window.createIcon = function(layer: string): L.Icon | L.DivIcon {
    if (layer === 'traffic') {
        return L.divIcon({
            html: '<span style="font-size:22px;line-height:25px;">⛔</span>',
            className: 'traffic-emoji-icon',
            iconSize: [25, 25],
            iconAnchor: [12.5, 12.5],
            popupAnchor: [0, -20]
        });
    }
    const config = ICON_CONFIG[layer] || ICON_CONFIG.pig;
    return L.icon({
        iconUrl: config.url,
        iconSize: config.size,
        iconAnchor: [12.5, 12.5],
        popupAnchor: [0, -20]
    });
};

window.createMarker = function(_map: L.Map, latLng: L.LatLng, properties: Record<string, unknown>): L.Marker {
    const marker = L.marker(latLng, { icon: window.createIcon(properties.layer as string) });
    marker.bindPopup(window.createPopupContent(properties));
    return marker;
};

function getGeometryColors(properties: Record<string, unknown>): { color: string; fillColor: string } {
    const raw = properties.time ?? properties.created_at ?? properties.timestamp;
    if (raw == null) return { color: '#dc3545', fillColor: '#dc3545' };
    let value: string | number = raw as string | number;
    if (typeof value === 'string') {
        let normalized = value.trim().replace(' ', 'T');
        if (!/([zZ]|[+-]\d{2}:?\d{2})$/.test(normalized)) normalized += 'Z';
        value = normalized;
    }
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return { color: '#dc3545', fillColor: '#dc3545' };
    const ageMs = window.serverNow() - d.getTime();
    if (ageMs <= 15 * 60 * 1000) return { color: '#dc3545', fillColor: '#dc3545' };
    if (ageMs <= 30 * 60 * 1000) return { color: '#0d6efd', fillColor: '#0d6efd' };
    return { color: '#ffffff', fillColor: '#ffffff' };
}

window.createCircle = function(_map: L.Map, coords: number[], properties: Record<string, unknown>, strategy?: string): L.Layer[] {
    const latLng = L.latLng(coords[1], coords[0]);
    const marker = window.createMarker(_map, latLng, properties as Record<string, unknown>);

    // Стратегия 'random' — событие без точной привязки к местности.
    // Такие точки НЕ должны иметь радиуса (круга): радиус подразумевает
    // точность, которой у случайной точки нет. Только маркер.
    if (strategy === 'random') {
        return [marker]; // Только маркер, без круга
    }

    // Для маркеров с точным местоположением добавляем круг радиусом 200м
    const colors = getGeometryColors(properties);
    const circle = L.circle(latLng, {
        color: colors.color,
        fillColor: colors.fillColor,
        fillOpacity: 0.5,
        radius: 200,
        weight: 0
    });
    circle.bindPopup(window.createPopupContent(properties));

    return [circle, marker];
};

window.getPolylineMidpoint = function(latLngs: L.LatLng[]): L.LatLng | null {
    if (!latLngs?.length) return null;
    if (latLngs.length === 1) return latLngs[0];

    let totalDistance = 0;
    const distances: number[] = [];

    for (let i = 0; i < latLngs.length - 1; i++) {
        const dist = latLngs[i].distanceTo(latLngs[i + 1]);
        distances.push(dist);
        totalDistance += dist;
    }

    const midDistance = totalDistance / 2;
    let distanceCovered = 0;

    for (let i = 0; i < distances.length; i++) {
        if (distanceCovered + distances[i] >= midDistance) {
            const ratio = (midDistance - distanceCovered) / distances[i];
            const p1 = latLngs[i];
            const p2 = latLngs[i + 1];
            return L.latLng(
                p1.lat + (p2.lat - p1.lat) * ratio,
                p1.lng + (p2.lng - p1.lng) * ratio
            );
        }
        distanceCovered += distances[i];
    }

    return latLngs[latLngs.length - 1];
};

window.createPolyline = function(map: L.Map, coords: [number, number][], properties: Record<string, unknown>): L.Layer[] {
    const latLngs = coords.map((c: [number, number]) => L.latLng(c[1], c[0]));
    const colors = getGeometryColors(properties);
    const polyline = L.polyline(latLngs, { color: colors.color, weight: 3 });
    polyline.bindPopup(window.createPopupContent(properties));

    const markerPosition = window.getPolylineMidpoint(latLngs);
    const marker = window.createMarker(map, markerPosition!, properties);

    return [polyline, marker];
};

window.createPolygon = function(map: L.Map, coords: [number, number][][] , properties: Record<string, unknown>): L.Layer[] {
    // GeoJSON Polygon: coords = [ outerRing, hole1, ... ]. Передаём ВСЕ кольца
    // (поддержка дыр), а не только внешнее (было coords[0] — дыры терялись).
    const latLngs = coords.map((ring: [number, number][]) => ring.map((c: [number, number]) => L.latLng(c[1], c[0])));
    const colors = getGeometryColors(properties);
    const polygon = L.polygon(latLngs, { color: colors.color, weight: 2, fillColor: colors.fillColor, fillOpacity: 0.2 });
    polygon.bindPopup(window.createPopupContent(properties));

    const marker = window.createMarker(map, polygon.getBounds().getCenter(), properties);
    return [polygon, marker];
};

window.createMultiPolygon = function(map: L.Map, coords: [number, number][][][], properties: Record<string, unknown>): L.Layer[] {
    // GeoJSON MultiPolygon: coords = [ polygon, ... ], где polygon = [ ring, ... ].
    // L.polygon принимает массив полигонов (каждый — массив колец) → рисуем как
    // единый мультиполигон, сохраняя дыры. Один маркер в центре общих границ.
    const latLngs = coords.map((polygon: [number, number][][]) =>
        polygon.map((ring: [number, number][]) => ring.map((c: [number, number]) => L.latLng(c[1], c[0])))
    );
    const colors = getGeometryColors(properties);
    const polygon = L.polygon(latLngs, { color: colors.color, weight: 2, fillColor: colors.fillColor, fillOpacity: 0.2 });
    polygon.bindPopup(window.createPopupContent(properties));

    const marker = window.createMarker(map, polygon.getBounds().getCenter(), properties);
    return [polygon, marker];
};

window.createMultiLineString = function(map, coords, properties) {
    // GeoJSON MultiLineString: coords = [ line, ... ], где line = [ coord, ... ].
    const elements: L.Layer[] = [];
    let allLatLngs: L.LatLng[] = [];
    for (const line of coords) {
        const latLngs = line.map(c => L.latLng(c[1], c[0]));
        allLatLngs = allLatLngs.concat(latLngs);
        const colors = getGeometryColors(properties);
        const polyline = L.polyline(latLngs, { color: colors.color, weight: 3 });
        polyline.bindPopup(window.createPopupContent(properties));
        elements.push(polyline);
    }
    if (allLatLngs.length > 0) {
        const center = L.latLng(
            allLatLngs.reduce((s, ll) => s + ll.lat, 0) / allLatLngs.length,
            allLatLngs.reduce((s, ll) => s + ll.lng, 0) / allLatLngs.length
        );
        const marker = window.createMarker(map, center, properties);
        elements.push(marker);
    }
    return elements;
};

window.createMultiPoint = function(map, coords, properties) {
    // GeoJSON MultiPoint: coords = [ coord, ... ].
    const elements: L.Layer[] = [];
    const allLatLngs: L.LatLng[] = [];
    for (const c of coords) {
        const latLng = L.latLng(c[1], c[0]);
        allLatLngs.push(latLng);
        const strategy = properties.strategy as string | undefined;
        const markerEls = window.createCircle(map, c, properties, strategy);
        elements.push(...markerEls);
    }
    return elements;
};

window.createGeometryCollection = function(map, coords, properties) {
    // GeoJSON GeometryCollection: coords = [ geometry, ... ].
    const elements: L.Layer[] = [];
    for (const geom of coords) {
        if (!geom || !geom.type) continue;
        const type = geom.type;
        const c = geom.coordinates;
        let created;
        switch (type) {
            case 'Point':
                created = window.createCircle(map, c, properties, properties.strategy as string | undefined);
                break;
            case 'LineString':
                created = window.createPolyline(map, c, properties);
                break;
            case 'Polygon':
                created = window.createPolygon(map, c, properties);
                break;
            case 'MultiPoint':
                created = window.createMultiPoint(map, c, properties);
                break;
            case 'MultiLineString':
                created = window.createMultiLineString(map, c, properties);
                break;
            case 'MultiPolygon':
                created = window.createMultiPolygon(map, c, properties);
                break;
            default:
                continue;
        }
        if (created) elements.push(...created);
    }
    return elements;
};

// Оборачивает слова matched_text в <strong> в уже HTML-экранированном тексте.
// matched_text — лемматизированный n-грамм ("молодёжный виноградово"),
// совпавший фрагмент сообщения (surface из матчера).
// Стемный regex: первые max(4, len-2) символов слова леммы + любой суффикс
// → "молодёжн" совпадает с "молодёжной" в description.
function _highlightMatchedParts(escapedText: string, matches: Array<Record<string, unknown>> | undefined): string {
    if (!matches || !matches.length) return escapedText;
    const parts = [...new Set(
        matches.map(m => (m.matched_text as string) || (m.matched_part as string))
            .filter(p => p && p.trim().length > 1)
    )];
    let result = escapedText;
    for (const part of parts) {
        for (const word of part.split(/\s+/)) {
            if (word.length < 4) continue;
            const stem = word.slice(0, Math.max(word.length - 2, 4));
            const stemEsc = stem.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            const regex = new RegExp(
                '(?<![а-яёА-ЯЁa-zA-Z0-9])' + stemEsc + '[а-яёА-ЯЁa-zA-Z0-9]*(?![а-яёА-ЯЁa-zA-Z0-9])',
                'gi'
            );
            result = result.replace(regex, (m: string) => `<strong>${m}</strong>`);
        }
    }
    return result;
}

/**
 * Sanitizes URL to prevent XSS attacks.
 * Only allows relative URLs starting with /media/events/ or /api/media/
 * and absolute HTTPS URLs from whitelisted domains.
 * 
 * @param url - URL to sanitize
 * @returns Sanitized URL or empty string if invalid/dangerous
 */
function sanitizeUrl(url: unknown): string {
    if (typeof url !== 'string' || !url) {
        return '';
    }
    
    const trimmedUrl = url.trim();
    
    // Block dangerous protocols that could execute JavaScript
    const dangerousProtocols = /^(javascript:|data:|vbscript:|file:|about:)/i;
    if (dangerousProtocols.test(trimmedUrl)) {
        console.warn('[sanitizeUrl] Blocked dangerous protocol:', trimmedUrl.substring(0, 50));
        return '';
    }
    
    // Allow relative URLs from our media endpoints
    if (trimmedUrl.startsWith('/media/events/') || trimmedUrl.startsWith('/api/media/')) {
        if (trimmedUrl.includes('..') || trimmedUrl.includes('%2e') || trimmedUrl.includes('%2f') || trimmedUrl.includes('%5c')) {
            console.warn('[sanitizeUrl] Blocked path traversal attempt:', trimmedUrl.substring(0, 50));
            return '';
        }
        return trimmedUrl;
    }
    
    // For absolute URLs, only allow HTTPS from trusted domains
    try {
        const parsedUrl = new URL(trimmedUrl);
        if (parsedUrl.protocol !== 'https:') {
            console.warn('[sanitizeUrl] Blocked non-HTTPS URL:', parsedUrl.protocol);
            return '';
        }
        // Allow if it's a relative-looking URL that got parsed (shouldn't happen often)
        return trimmedUrl;
    } catch {
        // If URL parsing fails, it might be a malformed URL or relative path
        // Only allow if it starts with a safe path
        if (trimmedUrl.startsWith('/')) {
            console.warn('[sanitizeUrl] Blocked unparseable relative URL:', trimmedUrl.substring(0, 50));
        }
        return '';
    }
}

window.createPopupContent = function(properties: Record<string, unknown>): string {
    if (!properties) return '';

    const time = properties.time ? window.formatDateTime(properties.time as string) : '';
    const description = properties.description ? (() => {
        const escaped = window.processTelegramHTML(properties.description as string);
        return _highlightMatchedParts(escaped, properties.matches as Array<Record<string, unknown>> | undefined);
    })() : '';

    const photoUrl = sanitizeUrl(properties.photo_url);
    const photoHtml = photoUrl ?
        `<div style="margin-top: 8px;"><img src="${photoUrl}" style="width: auto; max-width: 100%; height: auto; max-height: 80vh; border-radius: 8px;" alt="Event photo"></div>` : '';

    const timeHtml = time ? `<span style="font-weight: bold; display: block; margin-bottom: 4px;">${time}</span>` : '';

    return `<div class="photo-container" style="text-align: center; max-width: 360px; color: var(--tg-text-color, #000000); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
        ${timeHtml}
        ${photoHtml}
        ${description}
    </div>`;
};

window.createTelegramPopup = function(content: string, customOptions: Record<string, unknown> = {}): L.Popup {
    const popup = L.popup({ 
        minWidth: 200,
        maxWidth: 400,
        closeButton: true,
        autoClose: false,
        closeOnEscapeKey: true,
        closeOnClick: true,
        className: 'tg-styled-popup',
        offset: [0, 0] as [number, number],
        autoPanPadding: [50, 50] as [number, number],
        ...customOptions
    } as L.PopupOptions);
    popup.setContent(content);
    return popup;
};

