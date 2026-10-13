"""
PyPI Proxy — download bridge for TradGPT.

Deploy on Render (free tier). Exposes GET-only endpoints that fetch Python
wheels from PyPI and serve them back as binary downloads. This exists because
the TradGPT pod has no outbound internet; Render does.

Endpoints (all under /<PREFIX>/):
  /                     dashboard: list available packages
  /health               liveness check (JSON)
  /download/<package>   download a wheel (.whl) for the given package name
                        ?version=X to pin a version (default: latest)
  /list/<package>       list available versions + wheel URLs (JSON)
  /b64/<package>        fetch a wheel and return it as base64 in a JSON body
                        (?version=X to pin). Use this when the client can only
                        read text, not binary.

Caching:
  Wheels are cached on local disk after first download (WHEEL_CACHE_DIR env
  var, default ./wheel_cache). First request for a package is slow (PyPI
  fetch); every later request serves from disk in milliseconds. On Render
  free tier the disk is ephemeral — a redeploy clears the cache.

Security:
  - Random 6-char path prefix per deploy (set PREFIX env var to override)
  - Package names validated against ^[A-Za-z0-9._-]+$ (no path traversal)
  - Only .whl files are served (no .tar.gz source dists, no executables)
  - Simple in-memory rate limit per IP (20 req/min)
  - No credentials, no secrets, no mutation routes
"""

import base64
import os
import re
import secrets
import time
import json
from collections import defaultdict

import requests
from flask import Flask, Response, abort, request

app = Flask(__name__)

PREFIX = os.environ.get("PREFIX") or secrets.token_hex(3)  # 6 hex chars
BASE = f"/{PREFIX}"
PYPI_JSON = "https://pypi.org/pypi/{pkg}/json"
PYPI_SIMPLE = "https://pypi.org/simple/{pkg}/"

# Allowed package name: letters, digits, dot, underscore, hyphen
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _valid_pkg(name: str) -> bool:
    return bool(PKG_RE.match(name)) and len(name) <= 100


# ---------------------------------------------------------------------------
# Rate limiting (in-memory, per IP; fine for single-instance free tier)
# ---------------------------------------------------------------------------
_hits = defaultdict(list)


def _rate_limit(ip: str) -> bool:
    now = time.time()
    window = [t for t in _hits[ip] if now - t < 60]
    _hits[ip] = window
    return len(window) < 20


@app.before_request
def _check():
    ip = request.remote_addr or "unknown"
    if not _rate_limit(ip):
        abort(429)


def _err(msg: str, code: int = 502) -> Response:
    return Response(f"<pre>{msg}</pre>", status=code, mimetype="text/html")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route(f"{BASE}/")
def dashboard():
    body = (
        "<h1>PyPI Proxy</h1>"
        "<p>Download Python wheels from PyPI via this bridge.</p>"
        "<p><b>Usage:</b></p>"
        "<ul>"
        f"<li><a href='{BASE}/health'>/health</a> — liveness check (JSON)</li>"
        f"<li><a href='{BASE}/list/flask'>/list/&lt;package&gt;</a> — list versions</li>"
        f"<li><a href='{BASE}/download/flask'>/download/&lt;package&gt;</a> — download latest wheel</li>"
        f"<li><a href='{BASE}/download/flask?version=3.1.2'>/download/&lt;package&gt;?version=X</a> — pinned</li>"
        f"<li><a href='{BASE}/b64/flask'>/b64/&lt;package&gt;</a> — wheel as base64 JSON (text-safe)</li>"
        "</ul>"
        "<p>Only .whl files are served. Package names must match "
        "[A-Za-z0-9._-]+ (max 100 chars).</p>"
    )
    return Response(body, mimetype="text/html")


@app.route(f"{BASE}/health")
def health():
    return Response(
        json.dumps({
            "ok": True,
            "service": "pypi-proxy",
            "prefix": BASE + "/",
            "endpoints": ["/", "/health", "/list/<pkg>", "/download/<pkg>", "/b64/<pkg>"],
        }),
        mimetype="application/json",
    )


@app.route(f"{BASE}/list/<path:pkg>")
def list_versions(pkg: str):
    if not _valid_pkg(pkg):
        abort(400)
    url = PYPI_JSON.format(pkg=pkg)
    try:
        r = requests.get(url, timeout=30)
    except Exception as e:
        return _err(f"PyPI unreachable: {type(e).__name__}: {e}")
    if r.status_code == 404:
        return _err(f"Package '{pkg}' not found on PyPI", 404)
    if r.status_code != 200:
        return _err(f"PyPI returned HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    releases = data.get("releases", {})
    # Collect all wheel URLs with their versions
    wheels = []
    for ver, files in sorted(releases.items()):
        for f in files:
            if f["filename"].endswith(".whl"):
                wheels.append({
                    "version": ver,
                    "filename": f["filename"],
                    "url": f["url"],
                    "python": f.get("requires_python"),
                })
    body = (
        f"<h1>{data.get('name', pkg)} — available wheels</h1>"
        f"<p>Summary: {data.get('summary', '')}</p>"
        f"<p>{len(wheels)} wheel(s) across {len(releases)} release(s)</p>"
        f"<pre>{json.dumps(wheels[:50], indent=2)}</pre>"
        f"<p><a href='{BASE}/'>back</a></p>"
    )
    return Response(body, mimetype="text/html")


def _pick_wheel(pkg: str, version: str):
    """Fetch PyPI metadata and pick the target wheel.

    Returns (wheel_dict, None) on success or (None, error_Response) on failure.
    """
    url = PYPI_JSON.format(pkg=pkg)
    try:
        r = requests.get(url, timeout=30)
    except Exception as e:
        return None, _err(f"PyPI unreachable: {type(e).__name__}: {e}")
    if r.status_code == 404:
        return None, _err(f"Package '{pkg}' not found on PyPI", 404)
    if r.status_code != 200:
        return None, _err(f"PyPI returned HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    releases = data.get("releases", {})

    # Pick the target version
    if version:
        if version not in releases:
            return None, _err(f"Version {version} not found for {pkg}", 404)
        candidates = releases[version]
    else:
        # Use the 'urls' field which points to the latest release
        candidates = data.get("urls", [])
        if not candidates:
            # Fall back to highest version key
            if not releases:
                return None, _err(f"No releases found for {pkg}", 404)
            top_ver = sorted(releases.keys())[-1]
            candidates = releases[top_ver]

    # Find a wheel among the candidates
    for f in candidates:
        if f["filename"].endswith(".whl"):
            return f, None
    return None, _err(
        f"No .whl file found for {pkg} "
        f"(versions checked: {[f['filename'] for f in candidates[:5]]}). "
        "This package may only ship source distributions."
    )


@app.route(f"{BASE}/download/<path:pkg>")
def download_wheel(pkg: str):
    if not _valid_pkg(pkg):
        abort(400)
    version = request.args.get("version", "").strip()
    wheel, err = _pick_wheel(pkg, version)
    if err is not None:
        return err

    # Download the wheel binary
    try:
        wr = requests.get(wheel["url"], timeout=60, stream=True)
    except Exception as e:
        return _err(f"Wheel download failed: {type(e).__name__}: {e}")
    if wr.status_code != 200:
        return _err(f"Wheel fetch returned HTTP {wr.status_code}", 502)

    content = wr.content
    filename = wheel["filename"]
    return Response(
        content,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
            "X-Package": pkg,
            "X-Version": wheel.get("version", "?"),
        },
    )


B64_MAX_BYTES = 20 * 1024 * 1024  # 20 MB cap per wheel for the text endpoint

WHEEL_CACHE_DIR = os.environ.get("WHEEL_CACHE_DIR", os.path.join(os.getcwd(), "wheel_cache"))
os.makedirs(WHEEL_CACHE_DIR, exist_ok=True)


def _fetch_wheel_bytes(wheel: dict):
    """Download a wheel, using the local disk cache when possible.

    Returns (content, from_cache).
    """
    filename = wheel["filename"]
    cache_path = os.path.join(WHEEL_CACHE_DIR, filename)
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
        with open(cache_path, "rb") as fh:
            return fh.read(), True
    wr = requests.get(wheel["url"], timeout=60)
    if wr.status_code != 200:
        raise IOError(f"Wheel fetch returned HTTP {wr.status_code}")
    content = wr.content
    tmp_path = cache_path + ".part"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(content)
        os.replace(tmp_path, cache_path)
    except OSError:
        pass  # cache is best-effort; serve the bytes anyway
    return content, False


@app.route(f"{BASE}/b64/<path:pkg>")
def b64_wheel(pkg: str):
    """Return the wheel as base64 inside a JSON body (text-safe transport)."""
    if not _valid_pkg(pkg):
        abort(400)
    version = request.args.get("version", "").strip()
    wheel, err = _pick_wheel(pkg, version)
    if err is not None:
        return err

    try:
        content, from_cache = _fetch_wheel_bytes(wheel)
    except Exception as e:
        return _err(f"Wheel download failed: {type(e).__name__}: {e}")

    if len(content) > B64_MAX_BYTES:
        return _err(
            f"Wheel too large for /b64/ ({len(content)} bytes > "
            f"{B64_MAX_BYTES} cap). Use /download/ instead.", 413
        )

    return Response(
        json.dumps({
            "ok": True,
            "package": pkg,
            "version": wheel.get("version", "?"),
            "filename": wheel["filename"],
            "size_bytes": len(content),
            "from_cache": from_cache,
            "b64": base64.b64encode(content).decode("ascii"),
        }),
        mimetype="text/html",  # text/html so web_extract processes it; body is still JSON
    )


# ---------------------------------------------------------------------------
# Error handlers — no stack traces
# ---------------------------------------------------------------------------


@app.errorhandler(404)
def _404(e):
    return _err("Not found", 404)


@app.errorhandler(429)
def _429(e):
    return _err("Rate limit exceeded (20 req/min). Try again shortly.", 429)


@app.errorhandler(Exception)
def _exc(e):
    return _err("Internal error (details hidden)", 500)


if __name__ == "__main__":
    print(f"PyPI proxy prefix: {BASE}/")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
