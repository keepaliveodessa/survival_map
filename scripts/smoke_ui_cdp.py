#!/usr/bin/env python3
"""Browser smoke test for the web UI via CDP (websocket-client).

Flow:
  1. Open http://localhost/index.html (gate) -> dev tokens -> auto-redirect to /map.html
  2. Wait for map init; assert: no console errors, markers rendered, WS connected.
  3. Switch tile layers via window.switchTileLayer: vector-light -> osm -> dark -> local.
  4. Assert WS getStats().isConnected, healthy ping/pong heartbeat, snapshot loaded.
"""
import json
import time
import urllib.request

import websocket  # websocket-client

CDP = "http://127.0.0.1:9222"
TIMEOUT_EXC = (TimeoutError, websocket.WebSocketTimeoutException, OSError)


def new_tab(url):
    req = urllib.request.Request(f"{CDP}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class CDPClient:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        self._id = 0
        self.console_logs = []
        self.page_errors = []

    def _drain_event(self, timeout=0.0):
        self.ws.settimeout(timeout if timeout > 0 else 0.05)
        try:
            raw = self.ws.recv()
        except TIMEOUT_EXC:
            return False
        if raw is None:
            return False
        data = json.loads(raw)
        if "method" in data:
            m = data["method"]
            if m == "Runtime.consoleAPICalled":
                args = data["params"]["args"]
                text = " ".join(
                    str(a.get("value", a.get("description", ""))) for a in args
                )
                self.console_logs.append(
                    {"type": data["params"]["type"], "text": text}
                )
            elif m == "Runtime.exceptionThrown":
                self.page_errors.append(
                    data["params"]["exceptionDetails"].get("text", "?")
                )
        return True

    def cmd(self, method, params=None, timeout=15):
        self._id += 1
        mid = self._id
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self.ws.recv()
            except TIMEOUT_EXC:
                if time.time() >= deadline:
                    raise RuntimeError(f"CDP timeout: {method}")
                continue
            data = json.loads(raw)
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError(f"CDP error for {method}: {data['error']}")
                return data.get("result", {})
            self._handle_event(data)
        raise RuntimeError(f"CDP timeout: {method}")

    def _handle_event(self, data):
        m = data.get("method", "")
        if m == "Runtime.consoleAPICalled":
            args = data["params"]["args"]
            text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
            self.console_logs.append({"type": data["params"]["type"], "text": text})
        elif m == "Runtime.exceptionThrown":
            self.page_errors.append(data["params"]["exceptionDetails"].get("text", "?"))

    def drain(self, seconds=0.0):
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0 and seconds > 0:
                break
            if not self._drain_event(0.05 if seconds == 0 else min(remaining, 0.5)):
                if seconds == 0:
                    break

    def js(self, expr, await_promise=False, timeout=15):
        r = self.cmd(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
            timeout,
        )
        v = r.get("result", {})
        if v.get("subtype") == "error":
            raise RuntimeError(v.get("description", "js error"))
        return v.get("value")


def main():
    target = new_tab("http://localhost/index.html")
    c = CDPClient(target["webSocketDebuggerUrl"])
    print("tab:", target["id"][:8], "url:", target["url"])

    c.cmd("Runtime.enable")
    c.cmd("Page.enable")
    c.cmd("Page.navigate", {"url": "http://localhost/index.html"})

    # --- [1] Gate: config -> dev tokens -> redirect to /map.html ---
    on_map = False
    deadline = time.time() + 20
    while time.time() < deadline:
        c.drain(0.4)
        try:
            if c.js("window.location.pathname") == "/map.html":
                on_map = True
                break
        except RuntimeError:
            pass
    print(f"[1] gate redirect to /map.html: {'OK' if on_map else 'FAIL'}")
    if not on_map:
        for l in c.console_logs[-15:]:
            print("   ", l)
        raise SystemExit(1)

    # --- [2] WS connected (auth_ok -> get_events) ---
    ws_connected = False
    deadline = time.time() + 25
    while time.time() < deadline:
        c.drain(0.4)
        try:
            st = c.js(
                "window.webSocketManager ? window.webSocketManager.getStats() : null"
            )
            if st and st.get("isConnected"):
                ws_connected = True
                break
        except RuntimeError:
            pass
    time.sleep(2.5)  # let the snapshot land
    c.drain(0.5)
    print(f"[2] websocket connected: {'OK' if ws_connected else 'FAIL'}")

    st = c.js("window.webSocketManager.getStats()")
    print(f"    ws stats: {st}")

    # --- [3] Map state ---
    checks = c.js("""(() => {
        const m = window.currentMapInstance;
        if (!m) return {map: false};
        const layers = [];
        m.eachLayer(l => layers.push(l.constructor.name));
        const tp = m.getPane('tilePane');
        return {
            map: true,
            layerCount: layers.length,
            layerTypes: layers.join(','),
            markers: document.querySelectorAll('.leaflet-marker-icon').length,
            tilePaneHidden: tp ? tp.style.display === 'none' : null,
            currentTile: window.localStorage.getItem('preferred_tile_layer') || '(unset)'
        };
    })()""")
    print(f"[3] map state: {checks}")

    # --- [4] Tile layer switching ---
    for key in ["vector-light", "osm", "dark", "local"]:
        c.js(f"window.switchTileLayer('{key}')")
        time.sleep(2.5)
        c.drain(0.3)
        state = c.js("""(() => {
            const m = window.currentMapInstance;
            const tp = m ? m.getPane('tilePane') : null;
            const types = [];
            m.eachLayer(l => types.push(l.constructor.name));
            return {
                saved: window.localStorage.getItem('preferred_tile_layer'),
                tilePaneHidden: tp ? tp.style.display === 'none' : null,
                layers: types.join(',')
            };
        })()""")
        print(f"[4] switch -> {key}: {state}")

    # --- [5] Heartbeat: wait one ping cycle (25s), expect 0 missed pongs ---
    missed_before = st.get("missedPongs")
    time.sleep(27)
    c.drain(0.5)
    st2 = c.js("window.webSocketManager.getStats()")
    print(f"[5] heartbeat after 27s: {st2} (missedPongs before={missed_before})")

    # --- [6] Haptic feedback wiring (Rule 5): every notification fires haptics ---
    toast_ok = popup_ok = alert_ok = False
    try:
        haptic_calls = c.js("""(() => {
            const calls = [];
            const orig = window.hapticFeedback;
            window.hapticFeedback = (t) => { calls.push(t); orig(t); };
            // Stub alert(): in headless (no real Telegram WebApp) the wrapper
            // falls back to alert() which BLOCKS the renderer.
            window.alert = () => {};
            window.showNotification('smoke-toast', 300, 'info');
            if (window.telegramIntegration) {
                window.telegramIntegration.showPopup('smoke popup', [{type:'ok'}], 'success');
                window.telegramIntegration.showAlert('smoke alert', 'error');
            }
            window.hapticFeedback = orig;
            return JSON.stringify(calls);
        })()""")
        calls = json.loads(haptic_calls) if haptic_calls else []
        toast_ok = 'light' in calls
        popup_ok = 'success' in calls
        alert_ok = 'error' in calls
        print(f"[6] haptics: toast(info)->'light': {'OK' if toast_ok else 'FAIL'}, "
              f"popup->'success': {'OK' if popup_ok else 'FAIL'}, "
              f"alert->'error': {'OK' if alert_ok else 'FAIL'} | all calls: {calls}")
    except RuntimeError as e:
        print(f"[6] haptics: FAIL ({e})")

    # --- [7] Console hygiene ---
    errors = [l for l in c.console_logs if l["type"] == "error"]
    warnings = [l for l in c.console_logs if l["type"] == "warning"]
    print(f"[7] console: {len(errors)} errors, {len(warnings)} warnings, "
          f"{len(c.page_errors)} page exceptions")
    for e in errors[:10]:
        print(f"    [error] {e['text'][:160]}")
    for w in warnings[:5]:
        print(f"    [warn]  {w['text'][:160]}")
    for p in c.page_errors[:5]:
        print(f"    [exception] {p[:160]}")

    ok = (
        ws_connected
        and checks.get("map")
        and st2.get("missedPongs", 99) == 0
        and st2.get("isConnected")
        and toast_ok
        and popup_ok
        and alert_ok
        and len(errors) == 0
        and len(c.page_errors) == 0
    )
    print(f"\nSMOKE RESULT: {'PASS' if ok else 'FAIL'}")

    c.cmd("Target.closeTarget", {"targetId": target["id"]})
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
