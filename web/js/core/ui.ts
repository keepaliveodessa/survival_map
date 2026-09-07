// ui.js — UI инициализация, управление картой, попапы, оверлеи
// Архитектурно оптимизированная версия для Telegram Mini Apps

// Leaflet is loaded via <script> tag — available as UMD global.
// The import below gives us type info without a runtime bundle.
declare const L: typeof import('leaflet');

// Инициализация глобальных переменных
window.adSquares = {};

// Helper: MapLibre GL map methods that need `this` binding preserved.
// Extracting methods from the object (e.g. `const fn = glMap.getStyle; fn()`)
// loses `this` → TypeError inside MapLibre internals.
interface GLMap {
    getStyle(): { layers: Array<Record<string, unknown>> };
    setLayoutProperty(id: string, prop: string, val: string): void;
    setPaintProperty(id: string, prop: string, val: string | number): void;
    isStyleLoaded(): boolean;
    once(event: string, fn: () => void): void;
}

// Общий шаг для light/dark тем: скрыть все текстовые слои (type: symbol).
// try/catch: getStyle() может упасть если карта ещё не полностью инициализирована
// во время первого render-цикла (initGL → onAdd → setStyle).
function _hideSymbolLayers(glMap: GLMap): void {
    try {
        const style = glMap.getStyle();
        if (!style || !style.layers) return;
        style.layers
            .filter((l: Record<string, unknown>) => l.type === 'symbol')
            .forEach((l: Record<string, unknown>) => {
                try {
                    glMap.setLayoutProperty(l.id as string, 'visibility', 'none');
                } catch (_e) { /* layer not ready */ }
            });
    } catch (_e) { /* map style not loaded yet */ }
}

// Скрывает все текстовые слои в уже загруженном MapLibre GL стиле (light-тема).
function hideMaplibreLabels(glMap: GLMap): void {
    _hideSymbolLayers(glMap);
}

function _applyDarkTheme(glMap: GLMap): void {
    _hideSymbolLayers(glMap);
    try {
        const style = glMap.getStyle();
        if (!style || !style.layers) return;
        const layers = style.layers;

        layers.forEach(layer => {
            const id = ((layer.id as string) || '').toLowerCase();
            const sl = ((layer['source-layer'] as string) || '').toLowerCase();
            try {
                if (layer.type === 'background') {
                    glMap.setPaintProperty(layer.id as string, 'background-color', '#0d1b2e');
                } else if (layer.type === 'fill') {
                    if (id.includes('water') || sl === 'water' || sl.includes('water')) {
                        glMap.setPaintProperty(layer.id as string, 'fill-color', '#4db8d4');
                        glMap.setPaintProperty(layer.id as string, 'fill-opacity', 0.6);
                    } else if (id.includes('building') || sl === 'building') {
                        glMap.setPaintProperty(layer.id as string, 'fill-color', '#c8ccd0');
                        glMap.setPaintProperty(layer.id as string, 'fill-opacity', 0.25);
                    } else {
                        glMap.setPaintProperty(layer.id as string, 'fill-color', '#0d1b2e');
                    }
                } else if (layer.type === 'line') {
                    if (id.includes('water') || sl === 'water') {
                        glMap.setPaintProperty(layer.id as string, 'line-color', '#4db8d4');
                    } else if (id.includes('motorway') || id.includes('trunk') || id.includes('primary')) {
                        glMap.setPaintProperty(layer.id as string, 'line-color', '#3a7bd5');
                    } else if (id.includes('secondary') || id.includes('tertiary')) {
                        glMap.setPaintProperty(layer.id as string, 'line-color', '#5b9bd5');
                    } else if (sl === 'transportation' || id.includes('road') || id.includes('street')) {
                        glMap.setPaintProperty(layer.id as string, 'line-color', '#2d5a8e');
                    }
                }
            } catch (_e) { /* ignore paint errors */ }
        });
    } catch (_e) { /* map style not loaded yet */ }
}

// Helper: apply theme to a MapLibre GL map.
// Uses 'idle' event which fires when MapLibre has finished rendering and
// all tiles are loaded — more reliable than setTimeout or 'load' event
// (which fires before the map's internal style is fully initialized).
function _applyThemeToGLMap(glMap: GLMap, theme?: string): void {
    const apply = (): void => {
        if (theme === 'dark') {
            _applyDarkTheme(glMap);
        } else {
            hideMaplibreLabels(glMap);
        }
    };
    if (glMap.isStyleLoaded()) {
        glMap.once('idle', apply);
    } else {
        glMap.once('load', () => glMap.once('idle', apply));
    }
}

// Helper: создать MapLibre GL слой, добавить на карту и применить тему.
// Общий для switchTileLayer и initializeMap — раньше triple-копипаста
// «maplibreGL → addTo → isStyleLoaded/once('load')» расходилась при правках.
function _addMaplibreLayer(map: L.Map, style: string, theme?: string): L.Layer {
    const layer = (L as unknown as { maplibreGL: (opts: { style: string }) => L.Layer }).maplibreGL({ style });
    layer.addTo(map);
    const glMap = (layer as unknown as { getMaplibreMap: () => GLMap }).getMaplibreMap();
    if (glMap.isStyleLoaded()) {
        _applyThemeToGLMap(glMap, theme);
    } else {
        glMap.once('load', () => _applyThemeToGLMap(glMap, theme));
    }
    return layer;
}

// Доступные тайлы карт
interface TileProviderMaplibre {
    type: 'maplibre';
    style: string;
    theme?: string;
}
interface TileProviderOSM {
    type?: undefined;
    url: string;
    options: Record<string, unknown>;
}
interface TileProviderLocal {
    type: 'local';
}
type TileProvider = TileProviderMaplibre | TileProviderOSM | TileProviderLocal;

const TILE_PROVIDERS: Record<string, TileProvider> = {
    'local': {
        type: 'local'
    },
    'vector-light': {
        type: 'maplibre',
        style: 'https://tiles.openfreemap.org/styles/liberty'
    },
    'osm': {
        url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        options: {
            subdomains: 'abc',
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            crossOrigin: true
        }
    },
    'dark': {
        type: 'maplibre',
        style: 'https://tiles.openfreemap.org/styles/liberty',
        theme: 'dark'
    }
};

// Текущий активный тайл
let currentTileLayer: L.Layer | null = null;
let currentTileKey = 'local';

// Vector basemap layers (local GeoJSON)
let vectorBaseLayer: L.Layer | null = null;

let vectorTileIndex: any = null;

// Vector modules — static imports (bundled with ui.js, ~28KB geojson-vt + rendering)
import { loadVectorData } from './vector-data';
import { createVectorLayer, setVectorTheme } from './vector-layer';


// Функция для переключения тайлов
window.switchTileLayer = function(tileKey: string): void {
    if (!TILE_PROVIDERS[tileKey] || tileKey === currentTileKey) {
        return;
    }

    const map = window.currentMapInstance;
    if (!map) {
        console.error('[switchTileLayer] Map instance not available');
        return;
    }

    // Remove current layers
    if (currentTileLayer) {
        map.removeLayer(currentTileLayer);
        currentTileLayer = null;
    }
    // Remove vector layers if switching away from local
    if (vectorBaseLayer) {
        map.removeLayer(vectorBaseLayer);
        vectorBaseLayer = null;
    }

    // Add new layer
    const provider = TILE_PROVIDERS[tileKey];
    if (provider.type === 'local') {
        if (vectorTileIndex) {
            vectorBaseLayer = createVectorLayer(vectorTileIndex);
            vectorBaseLayer.addTo(map);
            const tp = map.getPane('tilePane');
            if (tp) tp.style.display = 'none';
        } else {
            (async () => {
                try {
                    const tileIdx = await loadVectorData();
                    vectorTileIndex = tileIdx;
                    vectorBaseLayer = createVectorLayer(tileIdx);
                    vectorBaseLayer.addTo(map);
                    const tp = map.getPane('tilePane');
                    if (tp) tp.style.display = 'none';
                } catch (err) {
                    console.warn('[switchTileLayer] Vector load failed:', err);
                    _addRasterFallback(map);
                }
            })();
        }
    } else if (provider.type === 'maplibre') {
        currentTileLayer = _addMaplibreLayer(map, (provider as TileProviderMaplibre).style, (provider as TileProviderMaplibre).theme);
    } else {
        const newLayer = L.tileLayer((provider as TileProviderOSM).url, { minZoom: 11, maxZoom: 19, ...(provider as TileProviderOSM).options });
        newLayer.addTo(map);
        (newLayer as L.TileLayer).bringToBack();
        currentTileLayer = newLayer;
    }

    currentTileKey = tileKey;

    // Сохраняем выбор в localStorage
    try {
        localStorage.setItem('preferred_tile_layer', tileKey);
    } catch (_e) {
        // Игнорируем ошибки localStorage
    }

    console.log('[switchTileLayer] Switched to:', tileKey);
};

// Инициализация карты и UI компонентов
window.initializeMap = function(): void {
    // Инициализация карты (Leaflet)
    const map = L.map('map', {
        attributionControl: false,
        zoomControl: true,
        preferCanvas: false,
        minZoom: 11,
        maxZoom: 18,
        maxBounds: L.latLngBounds([45.1, 28.1], [48.35, 31.4]),
        maxBoundsViscosity: 1.0,
    }).setView([window.APP_CONFIG.map_center_lat, window.APP_CONFIG.map_center_lng], window.APP_CONFIG.map_default_zoom);

    // Проверяем сохраненный выбор тайла
    try {
        const savedTile = localStorage.getItem('preferred_tile_layer');
        if (savedTile && savedTile in TILE_PROVIDERS) {
            currentTileKey = savedTile;
        } else if (savedTile) {
            console.log('[initializeMap] Clearing outdated tile key from localStorage:', savedTile);
            localStorage.removeItem('preferred_tile_layer');
        }
    } catch (_e) {
        // Игнорируем ошибки localStorage
    }

    // Добавляем выбранный тайл
    const provider = TILE_PROVIDERS[currentTileKey];
    if (provider.type === 'local') {
        const vectorLightProvider = TILE_PROVIDERS['vector-light'] as TileProviderMaplibre;
        currentTileLayer = _addMaplibreLayer(map, vectorLightProvider.style, vectorLightProvider.theme);

        // Local vector basemap — load async, swap placeholder when ready
        (async () => {
            try {
                const tileIdx = await loadVectorData();
                vectorTileIndex = tileIdx;

                // Respect mid-load switches: only swap if user still wants local
                if (currentTileKey !== 'local') return;

                // Remove placeholder and add local vector layer
                map.removeLayer(currentTileLayer);
                currentTileLayer = null;
                vectorBaseLayer = createVectorLayer(tileIdx);
                vectorBaseLayer.addTo(map);
                const tilePane = map.getPane('tilePane');
                if (tilePane) tilePane.style.display = 'none';
                console.log('[initializeMap] Local vector basemap loaded');
            } catch (err) {
                console.warn('[initializeMap] Vector data failed, keeping placeholder:', err);
                // Keep the placeholder as active base layer
                currentTileKey = 'vector-light';
                try {
                    localStorage.setItem('preferred_tile_layer', 'vector-light');
                } catch (_e) {
                    // Игнорируем ошибки localStorage
                }
            }
        })();
    } else if (provider.type === 'maplibre') {
        currentTileLayer = _addMaplibreLayer(map, (provider as TileProviderMaplibre).style, (provider as TileProviderMaplibre).theme);
    } else {
        currentTileLayer = L.tileLayer((provider as TileProviderOSM).url, { minZoom: 11, maxZoom: 19, ...(provider as TileProviderOSM).options });
        currentTileLayer.addTo(map);
    }

    window.setMapInstance(map);

    initializeMapLayers(map);
    initializeControls(map);
    addQuestionOverlay(map);
    initializeAdSquares(map);

    window.initializeWebSocket();
};

// Raster fallback when vector basemap fails to load
function _addRasterFallback(map: L.Map): void {
    if (currentTileLayer) return; // already have a tile layer
    currentTileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        subdomains: 'abc',
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        minZoom: 11, maxZoom: 19
    });
    currentTileLayer.addTo(map);
}

// Создание постоянных слоёв карты (один раз за сессию)
function initializeMapLayers(map: L.Map): void {
    window.markerClusterGroup = (L as unknown as { markerClusterGroup: (opts: Record<string, unknown>) => L.LayerGroup }).markerClusterGroup({
        chunkedLoading: true,
        maxClusterRadius: 50,
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
        zoomToBoundsOnClick: true,
        iconCreateFunction: function(cluster: { getChildCount(): number }) {
            const childCount = cluster.getChildCount();
            return new L.DivIcon({
                html: '<div style="background-color: rgba(255, 87, 51, 0.8); border-radius: 50%; width: 40px; height: 40px; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; font-size: 14px;">' + childCount + '</div>',
                className: 'marker-cluster-custom',
                iconSize: new L.Point(40, 40)
            });
        }
    });
    window.geometryLayerGroup = L.layerGroup();
    window.randomMarkersGroup = L.layerGroup();

    map.addLayer(window.markerClusterGroup);
    map.addLayer(window.geometryLayerGroup);
    map.addLayer(window.randomMarkersGroup);
}

// Функция для обновления индикатора статуса соединения
window.updateOnlineStatus = function(isOnline: boolean): void {
    const connectionIndicator = document.getElementById('connection-indicator');
    if (connectionIndicator) {
        connectionIndicator.style.display = isOnline ? 'none' : 'block';
        if (!isOnline) {
            connectionIndicator.textContent = '⚠️ Нет связи с сервером';
        }
    }
};

// Функция для инициализации контролов
// eslint-disable-next-line @typescript-eslint/no-unused-vars
function initializeControls(_map: L.Map): void {
    const controlsContainer = document.getElementById('controlsContainer');
    const controlsSlider = document.getElementById('controlsSlider');
    const indicators = document.querySelectorAll<HTMLElement>('#controlsIndicators .dot');

    if (!controlsContainer || !controlsSlider) return;
    const slider = controlsSlider; // guaranteed non-null below

    let startX = 0, currentX = 0, deltaX = 0, isSwiping = false, activePanel = 0;
    const panels = Array.from(slider.querySelectorAll('.controlPanel'));
    const panelCount = Math.max(1, panels.length);
    const stepPercent = 100 / panelCount;

    function setPanel(idx: number, _skipDataLoad: boolean = false): void { // eslint-disable-line @typescript-eslint/no-unused-vars
        activePanel = Math.min(Math.max(idx, 0), panelCount - 1);
        slider.style.transform = `translateX(-${activePanel * stepPercent}%)`;
        indicators.forEach((el, i) => el.classList.toggle('active', i === activePanel));
        window.hapticFeedback('selection_changed');
    }

    // Touch события
    controlsContainer.addEventListener('touchstart', (e: TouchEvent) => {
        if (e.touches.length !== 1) return;
        startX = e.touches[0].clientX;
        currentX = startX;
        isSwiping = true;
        slider.style.transition = 'none';
    }, { passive: true });

    controlsContainer.addEventListener('touchmove', (e: TouchEvent) => {
        if (!isSwiping) return;
        currentX = e.touches[0].clientX;
        deltaX = currentX - startX;
        slider.style.transform = `translateX(calc(-${activePanel * stepPercent}% + ${deltaX}px))`;
    }, { passive: true });

    controlsContainer.addEventListener('touchend', () => {
        if (!isSwiping) return;
        slider.style.transition = '';

        if (Math.abs(deltaX) > 40) {
            if (deltaX < 0 && activePanel < panelCount - 1) setPanel(activePanel + 1);
            else if (deltaX > 0 && activePanel > 0) setPanel(activePanel - 1);
            else setPanel(activePanel);
        } else {
            setPanel(activePanel);
        }

        isSwiping = false;
        deltaX = 0;
    }, { passive: true });

    // Индикаторы
    indicators.forEach((el, idx) => {
        el.addEventListener('click', () => setPanel(idx));
    });

    setPanel(0, true);

    // Фильтр времени
    const realtimeControls = document.querySelector('#realtimeControls .buttons');
    if (realtimeControls) {
        realtimeControls.addEventListener('click', (e: Event) => {
            const target = e.target as HTMLElement;
            if (target.tagName !== 'BUTTON') return;

            window.hapticFeedback('light');
            const newFilter = parseInt(target.dataset.minutes || '0', 10);
            realtimeControls.querySelectorAll('button').forEach(btn => btn.classList.remove('active'));
            target.classList.add('active');
            window.updateTimeFilter(newFilter);
        });

        const currentFilter = (window.store && window.store.getState)
            ? window.store.getState().currentTimeFilter
            : (window.DEFAULT_TIME_FILTER || 30);
        (realtimeControls.querySelector(`button[data-minutes="${currentFilter}"]`) as HTMLElement | null)?.classList.add('active');

        if (!realtimeControls.querySelector('.active')) {
            (realtimeControls.querySelector(`button[data-minutes="${window.DEFAULT_TIME_FILTER || 30}"]`) as HTMLElement | null)?.classList.add('active');
        }
    }

    // Фильтр слоёв
    const layerControls = document.querySelector('#layerControls .layers');
    if (layerControls) {
        const activeLayers = window.store?.getState().activeLayers;
        if (activeLayers) {
            layerControls.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                const checkbox = cb as HTMLInputElement;
                checkbox.checked = activeLayers.has((checkbox.dataset.layer || '') as import('../types/geojson').EventLayer);
            });
        }

        layerControls.addEventListener('change', (e: Event) => {
            const target = e.target as HTMLInputElement;
            if (target.tagName !== 'INPUT' || target.type !== 'checkbox') return;
            window.hapticFeedback('selection_changed');
            window.toggleLayerInStore(target.dataset.layer || '');
        });
    }

    // Переключение тайлов карты
    const tileControls = document.querySelector('#mapTileControls .tile-buttons');
    if (tileControls) {
        tileControls.addEventListener('click', (e: Event) => {
            const target = e.target as HTMLElement;
            if (target.tagName !== 'BUTTON') return;

            window.hapticFeedback('light');
            const tileKey = target.dataset.tile || '';

            tileControls.querySelectorAll('button').forEach(btn => btn.classList.remove('active'));
            target.classList.add('active');

            window.switchTileLayer(tileKey);
        });

        const activeTileButton = tileControls.querySelector(`button[data-tile="${currentTileKey}"]`);
        if (activeTileButton) {
            tileControls.querySelectorAll('button').forEach(btn => btn.classList.remove('active'));
            activeTileButton.classList.add('active');
        }
    }

    initializeInteractionControls();
}

// Функция для инициализации кнопок взаимодействия
function initializeInteractionControls(): void {
    const legendBtn = document.getElementById('legendBtn');
    const dayNightBtn = document.getElementById('dayNightBtn');
    const closeBtn = document.getElementById('closeCenterPopup');
    const overlay = document.getElementById('centerPopupOverlay');

    legendBtn?.addEventListener('click', () => {
        window.hapticFeedback('light');
        showLegendPopup();
    });

    dayNightBtn?.addEventListener('click', () => {
        window.hapticFeedback('light');
        toggleDayNightMode();
    });

    closeBtn?.addEventListener('click', () => {
        window.hapticFeedback('light');
        hideCenterPopup();
    });

    overlay?.addEventListener('click', () => {
        window.hapticFeedback('light');
        hideCenterPopup();
    });
}

// Функция переключения режима День/Ночь
let _isVectorDark = false;

function toggleDayNightMode(): void {
    if (currentTileKey === 'local' && vectorBaseLayer) {
        // Switch theme on the local vector basemap — no provider swap
        _isVectorDark = !_isVectorDark;
        const theme = _isVectorDark ? 'dark' : 'light';
        setVectorTheme(theme);
        console.log('[DayNight] Vector theme:', theme);
    } else {
        const isDarkMode = currentTileKey === 'dark';
        const newTileKey = isDarkMode ? 'vector-light' : 'dark';
        window.switchTileLayer(newTileKey);
        console.log('[DayNight] Switched to:', newTileKey);
    }
}

// Функция для добавления оверлея вопроса
function addQuestionOverlay(map: L.Map): void {
    const questionBounds = L.latLngBounds(
        [46.45304, 30.76985],
        [46.54304, 30.89285]
    );

    const overlayUrl = `/assets/images/question.svg?v=${Date.now()}`;

    const questionOverlay = L.imageOverlay(overlayUrl, questionBounds, {
        interactive: true,
        opacity: 1.0,
        zIndex: 1000
    }).addTo(map);

    questionOverlay.on('click', () => {
        const popup = window.createTelegramPopup(
            "Здесь отображаются события не имеющие привязки к местности, либо могут быть не точными!"
        );
        popup.setLatLng([46.49804, 30.83135]).openOn(map);
    });
}

// Функция для инициализации рекламных квадратов
function initializeAdSquares(map: L.Map): void {
    console.log('[initializeAdSquares] Starting banner initialization...');

    const bounds = L.latLngBounds([46.4370, 30.92288], [46.5240, 31.06208]);
    const imageUrl = '/assets/images/banner.svg';
    const fullUrl = imageUrl + '?v=' + Date.now();
    console.log('[initializeAdSquares] Banner URL:', fullUrl);

    const popupContent = `<h3>Исходный код приложения доступен на <a href="https://github.com/develop4alive/survival_map" target="_blank">GitHub</a></h3><br>поблагодарить разработчика можно на <a href="https://bastyon.com/keep_alive_odessa?ref=PHQHKADhBPxxSwjiggV6G2BxSvy6TY1Lgb" target="_blank">bastyon</a>`;

    if (!window.adSquares.ad1) {
        console.log('[initializeAdSquares] Creating image overlay...');

        const overlay = L.imageOverlay(fullUrl, bounds, {
            opacity: 1,
            interactive: true,
            pane: 'overlayPane',
            className: 'ad-overlay'
        }).addTo(map);

        console.log('[initializeAdSquares] Overlay added to map');

        overlay.bindPopup(popupContent, window.DEFAULT_POPUP_OPTIONS as Record<string, unknown>);
        window.adSquares.ad1 = overlay;
    } else {
        console.log('[initializeAdSquares] Banner already exists, skipping');
    }
}


// =============================================================================
// Инкрементный рендер карты
// =============================================================================

interface RenderedRecord {
    featureRef: import('../types/geojson').EventFeature;
    items: Array<{ layer: L.Layer; group: L.LayerGroup }>;
}
const renderedById = new Map<string | number, RenderedRecord>();
let isInitialRendering = false;

// Извлечение стабильного id из feature.
function featureId(feature: import('../types/geojson').EventFeature): string | number | null {
    const p = feature && feature.properties;
    if (!p) return null;
    if (p.id != null) return p.id;
    return null;
}

// Удаление всех слоёв, отрисованных для данного id.
function removeRenderedEvent(id: string | number): void {
    const record = renderedById.get(id);
    if (!record) return;
    for (const item of record.items) {
        try {
            item.group.removeLayer(item.layer);
        } catch (_e) {
            // Слой мог быть уже удалён
        }
    }
    renderedById.delete(id);
}

// Создание и добавление слоёв для одного feature.
function addRenderedEvent(id: string | number, feature: import('../types/geojson').EventFeature, map: L.Map): void {
    if (!feature.geometry) return;

    const props = feature.properties as Record<string, unknown>;
    let elements: L.Layer[];    const geoType = feature.geometry.type as string;
    switch (geoType) {
        case 'Point':
            elements = window.createCircle(map, (feature.geometry as import('geojson').Point).coordinates as [number, number], props, props.strategy as string);
            break;
        case 'LineString':
            elements = window.createPolyline(map, (feature.geometry as import('geojson').LineString).coordinates as [number, number][], props);
            break;
        case 'Polygon':
            elements = window.createPolygon(map, (feature.geometry as import('geojson').Polygon).coordinates as [number, number][][], props);
            break;
        case 'MultiPolygon':
            elements = window.createMultiPolygon(map, (feature.geometry as unknown as import('geojson').MultiPolygon).coordinates as [number, number][][][], props);
            break;
        case 'MultiLineString':
            elements = window.createMultiLineString(map, (feature.geometry as unknown as import('geojson').MultiLineString).coordinates as number[][][], feature.properties);
            break;
        case 'MultiPoint':
            elements = window.createMultiPoint(map, (feature.geometry as unknown as import('geojson').MultiPoint).coordinates as number[][], feature.properties);
            break;
        case 'GeometryCollection':
            // feature.geometry типизирован как Point | LineString | Polygon —
            // для GeometryCollection нужен каст к необработанным geometries.
            elements = window.createGeometryCollection(map, (feature.geometry as unknown as { geometries: Array<Record<string, unknown>> }).geometries, feature.properties);
            break;
        default:
            console.warn('[renderFromCache] Unsupported geometry type:', geoType);
            return;
    }

    const items: Array<{ layer: L.Layer; group: L.LayerGroup }> = [];
    for (const element of elements) {
        if (!element) continue;

        let group: L.LayerGroup;
        if (element instanceof L.Marker) {
            group = (props.strategy === 'random')
                ? window.randomMarkersGroup!
                : window.markerClusterGroup!;
        } else {
            group = window.geometryLayerGroup!;
        }

        group.addLayer(element);
        items.push({ layer: element, group: group });
    }

    renderedById.set(id, { featureRef: feature, items: items });
}

function _featureDistanceToCenter(feature: import('../types/geojson').EventFeature, center: L.LatLng): number {
    if (!feature.geometry || !feature.geometry.coordinates) return Infinity;

    const geometry = feature.geometry as any;
    const coords = geometry.coordinates as unknown[];
    const type = geometry.type;
    let representative: [number, number] | undefined;

    switch (type) {
        case 'Point':
            representative = coords as [number, number];
            break;
        case 'LineString':
            representative = (coords as [number, number][])[0];
            break;
        case 'Polygon':
            representative = (coords as [number, number][][])[0]?.[0];
            break;
        case 'MultiLineString':
            representative = (coords as [number, number][][])[0]?.[0];
            break;
        case 'MultiPoint':
            representative = (coords as [number, number][])[0];
            break;
        case 'MultiPolygon':
            representative = (coords as [number, number][][][])[0]?.[0]?.[0];
            break;
        default:
            return Infinity;
    }

    if (!representative) return Infinity;

    return L.latLng(representative[1], representative[0]).distanceTo(center);
}

// Инкрементная синхронизация карты с отфильтрованным набором событий из store.
window.renderFromCache = function(features?: import('../types/geojson').EventFeature[]): void {
    if (isInitialRendering && !features) {
        return;
    }
    const map = window.currentMapInstance;
    if (!map) {
        console.error('[renderFromCache] Map instance not available');
        return;
    }
    if (!window.markerClusterGroup || !window.geometryLayerGroup || !window.randomMarkersGroup) {
        console.error('[renderFromCache] Map layers not initialized');
        return;
    }

    const geoJsonData = window.getFilteredDataForRendering();
    const allFeatures = (geoJsonData && geoJsonData.features) ? geoJsonData.features : [];
    const featuresToRender = features || allFeatures;

    const nextIds = new Set<string | number>();
    let added = 0;
    let updated = 0;

    for (let i = 0; i < featuresToRender.length; i++) {
        const feature = featuresToRender[i] as import('../types/geojson').EventFeature;
        const id = featureId(feature);
        if (id == null) continue;

        nextIds.add(id);

        const existing = renderedById.get(id);
        if (existing && existing.featureRef === feature) {
            continue;
        }
        if (existing) {
            removeRenderedEvent(id);
            updated++;
        } else {
            added++;
        }
        addRenderedEvent(id, feature, map);
    }

    let removed = 0;
    if (!features) {
        for (const id of Array.from(renderedById.keys())) {
            if (!nextIds.has(id)) {
                removeRenderedEvent(id);
                removed++;
            }
        }
    }

    if (added || removed || updated) {
        console.log('[renderFromCache] diff:', { added, updated, removed, total: nextIds.size });
    }
};

function renderInitialBatch(): void {
    const geoJsonData = window.getFilteredDataForRendering();
    const features = (geoJsonData && geoJsonData.features) ? geoJsonData.features : [];
    if (!features.length) return;

    const map = window.currentMapInstance;
    if (!map) return;

    const center = map.getCenter();
    const sortedFeatures = [...features].sort((a, b) => {
        return _featureDistanceToCenter(a, center) - _featureDistanceToCenter(b, center);
    });

    const CHUNK_SIZE = 200;
    let index = 0;
    isInitialRendering = true;

    function processChunk(): void {
        const chunk = sortedFeatures.slice(index, index + CHUNK_SIZE);
        if (chunk.length === 0) {
            isInitialRendering = false;
            window.renderFromCache();
            console.log('[renderInitialBatch] Complete, total:', sortedFeatures.length);
            return;
        }

        window.renderFromCache(chunk);
        index += CHUNK_SIZE;

        if (index < sortedFeatures.length) {
            requestAnimationFrame(processChunk);
        } else {
            isInitialRendering = false;
            window.renderFromCache();
            console.log('[renderInitialBatch] Complete, total:', sortedFeatures.length);
        }
    }

    requestAnimationFrame(processChunk);
}

// Функция инициализации UI
window.bootstrapUI = function(): void {
    window.initializeMap();

    const iconUrls = (window.ICON_CONFIG as Record<string, { url: string }> | undefined)
        ? Object.values(window.ICON_CONFIG).map(c => c.url)
        : ['/assets/images/bus.webp', '/assets/images/pig.webp', '/assets/images/cops.webp'];
    window.preloadIcons(iconUrls);

    requestAnimationFrame(() => {
        renderInitialBatch();
    });
};
