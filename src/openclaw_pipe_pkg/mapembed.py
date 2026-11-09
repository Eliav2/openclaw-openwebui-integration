# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------

# The in-chat map. The Gateway plugin `map` (gateway-plugins/map in the
# openclaw-workspace repo) geocodes and routes the agent's stops and keeps the
# whole map under a `mapId`. A reply that called it gets ONE srcdoc embed: the
# stops list, the route line, a MapLibre map on OpenFreeMap tiles, and a static
# SVG underneath that stays when there is no WebGL or no network. There is no
# link out of the page (a link would open a window that keeps this origin's
# COOP): a stop has a copy-coordinates button instead.
#
# The map comes from `map.get`, not from the tool card: Open WebUI cuts a
# card's result at 8000 characters (`_tool_call_result_event`), and a 25-stop
# day plan with notes is longer. The card only has to name the map.

MAP_TOOL_NAME = "map"
MAP_GET_METHOD = "map.get"
MAP_GET_TIMEOUT_S = 5
_MAP_ID_RE = re.compile(r'"mapId"\s*:\s*"([0-9a-f]{16})"')

MAPLIBRE_VERSION = "5.6.0"
_MAPLIBRE_BASE = f"https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/{MAPLIBRE_VERSION}"
MAPLIBRE_JS_URL = f"{_MAPLIBRE_BASE}/maplibre-gl.js"
MAPLIBRE_CSS_URL = f"{_MAPLIBRE_BASE}/maplibre-gl.css"
MAPLIBRE_JS_SRI = ("sha512-BmEBFbeO0lgfo1PPX5M4B4zxlhZWhOP4Qb1Bn+NVtEI7W1ESwRrLPHpWYqw1WH3QgTqRDNsagD/"
                   "Fj8mjFCXvSQ==")
MAPLIBRE_CSS_SRI = ("sha512-DBNed2oEjOrfIRj8Czuzs7padWawLWbA64OoDekn8gs2a3J6WOaR5xXfnFgaxKGpxIuSn/"
                    "rAo/PsgOgO6PZ/vA==")
MAP_STYLE_URL = "https://tiles.openfreemap.org/styles/liberty"

MAP_HEIGHT_PX = 300
MAP_LOAD_TIMEOUT_MS = 10000
MAP_PADDING_PX = 36
MAP_MAX_ZOOM = 16
MAP_SINGLE_STOP_ZOOM = 14
MAP_COPIED_MS = 1500
# A day colour per group, in order of first appearance.
MAP_COLORS = ("#d9480f", "#1971c2", "#2f9e44", "#9c36b5", "#e8590c", "#0c8599", "#c2255c", "#5f3dc4")

_ITEM_CALL, _ITEM_CALL_OUTPUT = "function_call", "function_call_output"
_MIN_LINE_POINTS = 2
_SVG_WIDTH, _SVG_HEIGHT, _SVG_PAD = 600, 340, 34
_SVG_MAX_ROUTE_POINTS = 400
_SVG_STOP_RADIUS = 12
_POLYLINE_FACTOR = 100000
_POLYLINE_CHUNK_BITS = 5
_POLYLINE_CHUNK_MASK = 0x1F
_POLYLINE_CONTINUE = 0x20
_POLYLINE_OFFSET = 63
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_METERS_PER_KM = 1000


def _encode_polyline(points) -> str:
    """Encoded polyline (precision 5) of [lat, lon] pairs."""
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        la, lo = round(lat * _POLYLINE_FACTOR), round(lon * _POLYLINE_FACTOR)
        for value in (la - prev_lat, lo - prev_lon):
            v = ~(value << 1) if value < 0 else value << 1
            while v >= _POLYLINE_CONTINUE:
                out.append(chr((_POLYLINE_CONTINUE | (v & _POLYLINE_CHUNK_MASK)) + _POLYLINE_OFFSET))
                v >>= _POLYLINE_CHUNK_BITS
            out.append(chr(v + _POLYLINE_OFFSET))
        prev_lat, prev_lon = la, lo
    return "".join(out)


def _decode_polyline(text: str) -> list[list[float]]:
    points, index, lat, lon = [], 0, 0, 0

    def number() -> int:
        nonlocal index
        result, shift = 0, 0
        while True:
            byte = ord(text[index]) - _POLYLINE_OFFSET
            index += 1
            result |= (byte & _POLYLINE_CHUNK_MASK) << shift
            shift += _POLYLINE_CHUNK_BITS
            if byte < _POLYLINE_CONTINUE:
                break
        return ~(result >> 1) if result & 1 else result >> 1

    while index < len(text):
        lat += number()
        lon += number()
        points.append([lat / _POLYLINE_FACTOR, lon / _POLYLINE_FACTOR])
    return points


def _map_ids(output) -> list[str]:
    """The maps the reply's `map` calls made, in order. From the cards, so
    the inline reply, the turn writer and a rebuilt reply all find the same ones."""
    # A card has its call id in `id` (`_tool_call_started_event`); a stored one may use `call_id`.
    calls = {item.get("call_id") or item.get("id") for item in output or []
             if item.get("type") == _ITEM_CALL and _bare_tool_name(item.get("name") or "") == MAP_TOOL_NAME}
    ids: list[str] = []
    for item in output or []:
        if item.get("type") != _ITEM_CALL_OUTPUT or item.get("call_id") not in calls:
            continue
        text = "".join(part.get("text", "") for part in item.get("output") or [] if isinstance(part, dict))
        found = _MAP_ID_RE.search(text)
        if found and found.group(1) not in ids:
            ids.append(found.group(1))
    return ids


def _distance_text(meters: float) -> str:
    if meters < _METERS_PER_KM:
        return f"{round(meters)} m"
    return f"{meters / _METERS_PER_KM:.1f} km"


def _duration_text(seconds: float) -> str:
    minutes = round(seconds / _SECONDS_PER_MINUTE)
    hours, minutes = divmod(minutes, _MINUTES_PER_HOUR)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _json_for_script(value) -> str:
    """JSON that cannot end its own <script> block or the page."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    for raw, safe in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                      (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(raw, safe)
    return text


def _map_fallback_svg(stops: list[ResultStop], routes: list[GeometryRoute]) -> str:
    """Stops and route line without a basemap, drawn here so it needs no script and no network."""
    pts = [(s["lat"], s["lon"]) for s in stops]
    lines = [[(p[0], p[1]) for p in _decode_polyline(r["polyline"])] for r in routes]
    for line in lines:
        pts += line
    lat0, lat1 = min(p[0] for p in pts), max(p[0] for p in pts)
    lon0, lon1 = min(p[1] for p in pts), max(p[1] for p in pts)
    squeeze = math.cos(math.radians((lat0 + lat1) / 2))
    width, height = max((lon1 - lon0) * squeeze, 1e-9), max(lat1 - lat0, 1e-9)
    scale = min((_SVG_WIDTH - 2 * _SVG_PAD) / width, (_SVG_HEIGHT - 2 * _SVG_PAD) / height)
    left = (_SVG_WIDTH - width * scale) / 2
    top = (_SVG_HEIGHT - height * scale) / 2

    def at(lat, lon):
        return f"{left + (lon - lon0) * squeeze * scale:.1f},{top + (lat1 - lat) * scale:.1f}"

    parts = [f'<svg id="fallback" viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" role="img" '
             f'aria-label="Map without a basemap">']
    for route, line in zip(routes, lines):
        if len(line) < _MIN_LINE_POINTS:
            continue
        step = max(1, len(line) // _SVG_MAX_ROUTE_POINTS)
        shown = line[::step] + ([line[-1]] if (len(line) - 1) % step else [])
        parts.append(f'<polyline points="{" ".join(at(*p) for p in shown)}" fill="none" '
                     f'stroke="{_map_color(stops, route.get("group"))}" stroke-width="4" '
                     f'stroke-linejoin="round" stroke-linecap="round" opacity=".8"/>')
    for s in stops:
        x, y = at(s["lat"], s["lon"]).split(",")
        parts.append(f'<circle cx="{x}" cy="{y}" r="{_SVG_STOP_RADIUS}" fill="{_map_color(stops, s.get("group"))}" '
                     f'stroke="#fff" stroke-width="2"/>'
                     f'<text x="{x}" y="{y}" dy=".35em" text-anchor="middle" fill="#fff" font-size="13" '
                     f'font-weight="700">{s["n"]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _map_color(stops, group) -> str:
    groups = list(dict.fromkeys(s["group"] for s in stops if s.get("group")))
    return MAP_COLORS[groups.index(group) % len(MAP_COLORS)] if group in groups else MAP_COLORS[0]


_MAP_PAGE_CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1c1c1e;--muted:#6b6b70;--line:#e3e3e6;--chip:#f2f2f4}
@media (prefers-color-scheme:dark){:root{--bg:#1f1f22;--fg:#f2f2f4;--muted:#a0a0a8;--line:#38383d;--chip:#2c2c30}}
*{box-sizing:border-box}
body{margin:0;padding:12px;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
h1{margin:0 0 2px;font-size:17px}
.summary,.legend,.foot{margin:0 0 8px;color:var(--muted);font-size:13px}
.legend span{margin-right:12px;white-space:nowrap}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}
#map{position:relative;height:__HEIGHT__px;border-radius:12px;overflow:hidden;background:var(--chip);border:1px solid var(--line)}
#fallback,#mapview{position:absolute;inset:0;width:100%;height:100%}
#fallback{background:var(--chip)}
.pin{width:26px;height:26px;border-radius:50%;color:#fff;font:700 13px/26px system-ui,sans-serif;text-align:center;border:2px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.4)}
ol{list-style:none;margin:12px 0 0;padding:0}
.stop{display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-top:1px solid var(--line)}
.badge{flex:none;width:26px;height:26px;border-radius:50%;color:#fff;font-weight:700;text-align:center;line-height:26px}
.body{flex:1;min-width:0}
.label{font-weight:600;overflow-wrap:anywhere}
.sub{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.coords{font:12px ui-monospace,monospace;color:var(--muted)}
button.copy{flex:none;min-height:44px;min-width:44px;padding:0 12px;border:1px solid var(--line);border-radius:10px;background:var(--chip);color:var(--fg);font:inherit;font-size:13px}
.missing{margin:10px 0 0;color:var(--muted);font-size:13px}
"""

_MAP_PAGE_JS = """
(function () {
  var data = JSON.parse(document.getElementById('data').textContent);
  function tell() { parent.postMessage({type: 'iframe:height', height: document.documentElement.scrollHeight}, '*'); }
  if (window.ResizeObserver) new ResizeObserver(tell).observe(document.body);
  window.addEventListener('load', tell);
  tell();

  function legacyCopy(text) {
    var t = document.createElement('textarea');
    t.value = text; t.style.position = 'fixed'; t.style.opacity = '0';
    document.body.appendChild(t); t.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) {}
    t.remove();
    return ok;
  }
  document.querySelectorAll('button.copy').forEach(function (b) {
    b.addEventListener('click', function () {
      var text = b.getAttribute('data-coords');
      var done = function () { b.textContent = 'Copied'; setTimeout(function () { b.textContent = 'Copy'; }, __COPIED_MS__); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { if (legacyCopy(text)) done(); });
      } else if (legacyCopy(text)) { done(); }
    });
  });

  function hasWebGL() {
    try {
      var c = document.createElement('canvas');
      return !!(window.WebGLRenderingContext && (c.getContext('webgl2') || c.getContext('webgl')));
    } catch (e) { return false; }
  }
  function decode(s) {
    var pts = [], i = 0, lat = 0, lon = 0;
    function num() {
      var r = 0, sh = 0, b;
      do { b = s.charCodeAt(i++) - 63; r |= (b & 31) << sh; sh += 5; } while (b >= 32);
      return (r & 1) ? ~(r >> 1) : (r >> 1);
    }
    while (i < s.length) { lat += num(); lon += num(); pts.push([lon / 1e5, lat / 1e5]); }
    return pts;
  }
  var started = false;
  function start() {
    if (started || !window.maplibregl || !hasWebGL()) return;
    started = true;
    var view = document.createElement('div');
    view.id = 'mapview';
    view.style.visibility = 'hidden';
    document.getElementById('map').appendChild(view);
    var map;
    try {
      map = new maplibregl.Map({container: view, style: data.style, cooperativeGestures: true,
                                attributionControl: {compact: true}});
    } catch (e) { view.remove(); return; }
    var tileErrors = 0;
    // No map in time: dispose it (it holds a WebGL context) and keep the drawing.
    var giveUp = setTimeout(function () { map.remove(); view.remove(); }, __TIMEOUT_MS__);
    map.on('error', function (e) { if (e && e.sourceId) tileErrors++; });
    map.on('load', function () {
      var bounds = new maplibregl.LngLatBounds();
      data.routes.forEach(function (r, k) {
        var line = decode(r.polyline);
        line.forEach(function (p) { bounds.extend(p); });
        map.addSource('r' + k, {type: 'geojson', data: {type: 'Feature', geometry: {type: 'LineString', coordinates: line}}});
        map.addLayer({id: 'r' + k, type: 'line', source: 'r' + k, layout: {'line-join': 'round', 'line-cap': 'round'},
                      paint: {'line-color': r.color, 'line-width': 5, 'line-opacity': 0.85}});
      });
      data.stops.forEach(function (s) {
        var el = document.createElement('div');
        el.className = 'pin'; el.style.background = s.color; el.textContent = s.n;
        new maplibregl.Marker({element: el}).setLngLat([s.lon, s.lat]).addTo(map);
        bounds.extend([s.lon, s.lat]);
      });
      if (data.stops.length === 1 && !data.routes.length) {
        map.jumpTo({center: [data.stops[0].lon, data.stops[0].lat], zoom: __SINGLE_ZOOM__});
      } else {
        map.fitBounds(bounds, {padding: __PADDING__, maxZoom: __MAX_ZOOM__, duration: 0});
      }
      // The style can load while its tiles do not: the drawing goes only once the tiles are in.
      map.once('idle', function () {
        clearTimeout(giveUp);
        if (tileErrors || !map.areTilesLoaded()) { map.remove(); view.remove(); return; }
        view.style.visibility = 'visible';
        var fb = document.getElementById('fallback');
        if (fb) fb.style.display = 'none';
        tell();
      });
    });
  }
  start();
  document.getElementById('lib').addEventListener('load', start);
})();
"""


def _map_html(geo: MapGeometry) -> str:
    """The reply's map as one self-contained page, from a `map.get` reply."""
    stops, routes = geo.get("stops") or [], geo.get("routes") or []
    unresolved = geo.get("unresolved") or []
    esc = html.escape
    groups = list(dict.fromkeys(s["group"] for s in stops if s.get("group")))
    colored = [{**s, "color": _map_color(stops, s.get("group"))} for s in stops]
    lines = [{**r, "color": _map_color(stops, r.get("group"))} for r in routes]

    summary = []
    for r in routes:
        where = f"{esc(r['group'])} · " if r.get("group") else ""
        summary.append(f"{where}{esc(r['mode'])} · {_distance_text(r['distanceM'])} · {_duration_text(r['durationS'])}")
    legend = "".join(f'<span><i class="dot" style="background:{_map_color(stops, g)}"></i>{esc(g)}</span>'
                     for g in groups)
    items = []
    for s in colored:
        coords = f"{s['lat']},{s['lon']}"
        sub = "".join(f'<div class="sub">{esc(s[k])}</div>' for k in ("note", "place") if s.get(k))
        items.append(
            f'<li class="stop"><span class="badge" style="background:{s["color"]}">{s["n"]}</span>'
            f'<div class="body"><div class="label">{esc(s["label"])}</div>{sub}'
            f'<div class="coords">{esc(coords)}</div></div>'
            f'<button class="copy" type="button" data-coords="{esc(coords)}" '
            f'aria-label="Copy coordinates of {esc(s["label"])}">Copy</button></li>')
    missing = "".join(f"{esc(u['label'])} ({esc(u['reason'])}); " for u in unresolved)
    data = {"style": MAP_STYLE_URL,
            "stops": [{"n": s["n"], "lat": s["lat"], "lon": s["lon"], "color": s["color"]} for s in colored],
            "routes": [{"polyline": r["polyline"], "color": r["color"]} for r in lines]}
    js = (_MAP_PAGE_JS.replace("__COPIED_MS__", str(MAP_COPIED_MS))
          .replace("__TIMEOUT_MS__", str(MAP_LOAD_TIMEOUT_MS)).replace("__SINGLE_ZOOM__", str(MAP_SINGLE_STOP_ZOOM))
          .replace("__PADDING__", str(MAP_PADDING_PX)).replace("__MAX_ZOOM__", str(MAP_MAX_ZOOM)))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<link rel="stylesheet" href="{MAPLIBRE_CSS_URL}" integrity="{MAPLIBRE_CSS_SRI}" crossorigin="anonymous">'
        f'<style>{_MAP_PAGE_CSS.replace("__HEIGHT__", str(MAP_HEIGHT_PX))}</style></head><body>'
        f'<h1>{esc(geo.get("title") or "Map")}</h1>'
        f'<p class="summary">{"<br>".join(summary)}</p>'
        f'<p class="legend">{legend}</p>'
        f'<div id="map">{_map_fallback_svg(stops, routes) if stops else ""}</div>'
        f'<ol id="stops">{"".join(items)}</ol>'
        f'{f"""<p class="missing">Not placed: {missing}</p>""" if missing else ""}'
        '<p class="foot">Map data © OpenStreetMap contributors, OpenFreeMap.</p>'
        f'<script type="application/json" id="data">{_json_for_script(data)}</script>'
        f'<script src="{MAPLIBRE_JS_URL}" integrity="{MAPLIBRE_JS_SRI}" crossorigin="anonymous" async id="lib"></script>'
        f'<script>{js}</script></body></html>')


def _merge_embeds(saved, pages: list[str]) -> list[str]:
    """What a message's `embeds` holds after a write: the embeds it already had (a live view, a map
    the Gateway no longer has), then the pages not among them. A tab replaces its list with an
    event's, so a write and an event carry this whole list."""
    kept = [e for e in saved or [] if isinstance(e, str)]
    return kept + [p for p in pages if p not in kept]


async def _embed_fields(conn, output: list, saved) -> dict:
    """`embeds` for a message rebuilt from the transcript, or nothing when it has no map."""
    pages = await _map_embeds(conn, output, {})
    return {"embeds": _merge_embeds(saved, pages)} if pages else {}


async def _map_embeds(conn, output, seen: dict) -> list[str]:
    """One page per map the reply made. `seen` (map id -> page, or None for a map the
    Gateway no longer has) makes a map cost one request however often this runs."""
    ids = _map_ids(output)
    for map_id in ids:
        if map_id in seen:
            continue
        try:
            reply = await conn.send_request(MAP_GET_METHOD, {"mapId": map_id}, timeout=MAP_GET_TIMEOUT_S)
            seen[map_id] = _map_html(reply) if (reply or {}).get("ok") and reply.get("stops") else None
        except Exception as ex:
            pipe_log(f"  map: {map_id} not fetched: {ex!r}")
    return [seen[i] for i in ids if seen.get(i)]
