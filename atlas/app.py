"""Atlas - production map server.

The browser only talks to this server. This server talks to the map providers,
which gives us: caching, rate limiting, input validation, a proper User-Agent,
swappable providers (via env vars), and no third-party API keys in the browser.
"""
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict

import requests
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

# ---------- configuration (all via environment) ----------
CONTACT = os.getenv("CONTACT_EMAIL", "")  # REQUIRED by Nominatim/Overpass usage policy
UA = f"AtlasMaps/1.0 ({CONTACT or 'set CONTACT_EMAIL'})"
GEOCODE_URL = os.getenv("GEOCODE_URL", "https://nominatim.openstreetmap.org/search")
ROUTE_URL = os.getenv("ROUTE_URL", "https://router.project-osrm.org/route/v1/driving")
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
TILE_URL = os.getenv("TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
TILE_ATTR = os.getenv("TILE_ATTR", '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors')
GEOCODE_API_KEY = os.getenv("GEOCODE_API_KEY", "")  # optional, appended as &key=...
RATE_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "15"))
# Public Nominatim allows max 1 request/second overall.
GEOCODE_MIN_INTERVAL = float(os.getenv(
    "GEOCODE_MIN_INTERVAL", "1.1" if "nominatim.openstreetmap.org" in GEOCODE_URL else "0"))
MAX_WAYPOINTS = 7

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("atlas")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
# Trust one proxy hop (nginx / load balancer) so the real client IP is used for limiting.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
session = requests.Session()
session.headers["User-Agent"] = UA

if not CONTACT:
    log.warning("CONTACT_EMAIL is not set. Public OSM services require it; set it before launch.")
if "openstreetmap.org" in GEOCODE_URL or "project-osrm.org" in ROUTE_URL:
    log.warning("Using public demo endpoints. Fine for low traffic; use hosted/self-hosted providers for real load.")


# ---------- small thread-safe TTL cache + per-IP rate limiter ----------
class TTLCache:
    def __init__(self, max_items=5000):
        self.max, self.data, self.lock = max_items, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            hit = self.data.get(key)
            if hit and hit[0] > time.time():
                self.data.move_to_end(key)
                return hit[1]
            self.data.pop(key, None)

    def set(self, key, value, ttl):
        with self.lock:
            self.data[key] = (time.time() + ttl, value)
            self.data.move_to_end(key)
            while len(self.data) > self.max:
                self.data.popitem(last=False)


cache = TTLCache()
_hits, _hits_lock = {}, threading.Lock()


def rate_limited(ip):
    now = time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(ip, []) if now - t < 60]
        recent.append(now)
        _hits[ip] = recent
        if len(_hits) > 20000:  # prune stale IPs
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
                _hits.pop(k, None)
        return len(recent) > RATE_PER_MIN


_geo_lock, _geo_last = threading.Lock(), [0.0]


def throttle_geocode():
    if not GEOCODE_MIN_INTERVAL:
        return
    with _geo_lock:
        wait = _geo_last[0] + GEOCODE_MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _geo_last[0] = time.time()


@app.before_request
def limit():
    if request.path.startswith("/api/") and rate_limited(request.remote_addr):
        return jsonify(error="rate_limited"), 429, {"Retry-After": "30"}


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path == "/":
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com; "
            "img-src 'self' data: blob: https:; connect-src 'self'; frame-ancestors 'self'")
    return resp


def upstream(method, url, cache_key, ttl, **kw):
    """Call a provider with caching and uniform error handling."""
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        r = session.request(method, url, timeout=UPSTREAM_TIMEOUT, **kw)
    except requests.RequestException as e:
        log.error("upstream failure %s: %s", url.split("?")[0], e)
        raise UpstreamError(502)
    if r.status_code == 429:
        raise UpstreamError(503)
    if not r.ok:
        log.error("upstream %s returned %s", url.split("?")[0], r.status_code)
        raise UpstreamError(502)
    try:
        data = r.json()
    except ValueError:
        raise UpstreamError(502)
    cache.set(cache_key, data, ttl)
    return data


class UpstreamError(Exception):
    def __init__(self, status):
        self.status = status


@app.errorhandler(UpstreamError)
def upstream_error(e):
    return jsonify(error="upstream_unavailable"), e.status


# ---------- routes ----------
@app.route("/")
def home():
    html = render_template("index.html")
    html = html.replace("'__TILE_URL__'", json.dumps(TILE_URL)).replace("__TILE_ATTR__", json.dumps(TILE_ATTR))
    return Response(html, mimetype="text/html")


@app.route("/healthz")
def healthz():
    return jsonify(status="ok")


@app.route("/api/geocode")
def geocode():
    q = re.sub(r"\s+", " ", request.args.get("q", "")).strip()
    if not 2 <= len(q) <= 200:
        return jsonify(error="bad_query"), 400
    limit = min(max(request.args.get("limit", 5, type=int), 1), 10)
    key = f"g:{q.lower()}:{limit}"
    if (hit := cache.get(key)) is not None:
        return jsonify(hit)
    params = {"format": "jsonv2", "limit": limit, "q": q}
    if GEOCODE_API_KEY:
        params["key"] = GEOCODE_API_KEY
    throttle_geocode()
    data = upstream("GET", GEOCODE_URL, key, 24 * 3600, params=params)
    return jsonify(data)


@app.route("/api/route")
def route():
    pts = request.args.get("coords", "").split(";")
    if not 2 <= len(pts) <= 2 + MAX_WAYPOINTS:
        return jsonify(error="bad_coords"), 400
    clean = []
    for p in pts:
        try:
            lon, lat = (float(x) for x in p.split(","))
        except ValueError:
            return jsonify(error="bad_coords"), 400
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            return jsonify(error="bad_coords"), 400
        clean.append(f"{lon:.6f},{lat:.6f}")
    coords = ";".join(clean)
    data = upstream("GET", f"{ROUTE_URL}/{coords}", f"r:{coords}", 600,
                    params={"overview": "full", "geometries": "geojson",
                            "alternatives": "true", "steps": "true"})
    return jsonify(data)


# Only categories the UI offers; the Overpass query is built here, never by the client.
SAFE_TAG = re.compile(r"^[a-z_]{1,30}$")


@app.route("/api/nearby")
def nearby():
    key_, val = request.args.get("key", ""), request.args.get("value", "")
    lat, lng = request.args.get("lat", type=float), request.args.get("lng", type=float)
    if not (SAFE_TAG.match(key_) and SAFE_TAG.match(val)) or lat is None or lng is None \
            or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify(error="bad_request"), 400
    lat, lng = round(lat, 3), round(lng, 3)  # ~110 m grid: better cache hits
    q = f'[out:json][timeout:20];nwr(around:1800,{lat},{lng})["{key_}"="{val}"];out center 30;'
    data = upstream("POST", OVERPASS_URL, f"n:{key_}:{val}:{lat}:{lng}", 900, data={"data": q})
    return jsonify(data)


if __name__ == "__main__":  # dev only; production uses gunicorn
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=int(os.getenv("PORT", "8000")))
