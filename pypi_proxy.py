"""
PyPI Proxy — download bridge for TradGPT.

Deploy on Render (free tier). Exposes GET-only endpoints that fetch Python
wheels from PyPI and serve them back as binary downloads. This exists because
the TradGPT pod has no outbound internet; Render does.

Endpoints (all under /<PREFIX>/):
  /                     dashboard: list available packages
  /download/<package>   download a wheel (.whl) for the given package name
                        ?version=X to pin a version (default: latest)
  /list/<package>       list available versions + wheel URLs (JSON)

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
from flask import Flask, Response, abort, jsonify, request

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
        f"<li><a href='{BASE}/list/flask'>/list/&lt;package&gt;</a> — list versions</li>"
        f"<li><a href='{BASE}/download/flask'>/download/&lt;package&gt;</a> — download latest wheel</li>"
        f"<li><a href='{BASE}/download/flask?version=3.1.2'>/download/&lt;package&gt;?version=X</a> — pinned</li>"
        f"<li><a href='{BASE}/b64/flask'>/b64/&lt;package&gt;</a> — wheel as base64 JSON (for text-only fetchers)</li>"
        "</ul>"
        "<p>Only .whl files are served. Package names must match "
        "[A-Za-z0-9._-]+ (max 100 chars).</p>"
    )
    return Response(body, mimetype="text/html")


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


@app.route(f"{BASE}/download/<path:pkg>")
def download_wheel(pkg: str):
    if not _valid_pkg(pkg):
        abort(400)
    version = request.args.get("version", "").strip()
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

    # Pick the target version
    if version:
        if version not in releases:
            return _err(f"Version {version} not found for {pkg}", 404)
        candidates = releases[version]
    else:
        # Use the 'urls' field which points to the latest release
        candidates = data.get("urls", [])
        if not candidates:
            # Fall back to highest version key
            if not releases:
                return _err(f"No releases found for {pkg}", 404)
            top_ver = sorted(releases.keys())[-1]
            candidates = releases[top_ver]

    # Find a wheel among the candidates
    wheel = None
    for f in candidates:
        if f["filename"].endswith(".whl"):
            wheel = f
            break
    if not wheel:
        return _err(
            f"No .whl file found for {pkg} "
            f"(versions checked: {[f['filename'] for f in candidates[:5]]}). "
            "This package may only ship source distributions."
        )

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


@app.route(f"{BASE}/b64/<path:pkg>")
def download_wheel_b64(pkg: str):
    """Return the wheel as base64-encoded JSON — fetchable by text-only tools.

    Usage: /b64/flask  or  /b64/flask?version=3.1.2
    Response: {"ok": true, "package": "...", "version": "...",
               "filename": "...", "size_bytes": N, "b64": "<base64>"}
    """
    if not _valid_pkg(pkg):
        abort(400)
    version = request.args.get("version", "").strip()
    url = PYPI_JSON.format(pkg=pkg)
    try:
        r = requests.get(url, timeout=30)
    except Exception as e:
        return jsonify({"ok": False, "error": f"PyPI unreachable: {type(e).__name__}"}), 502
    if r.status_code == 404:
        return jsonify({"ok": False, "error": f"Package '{pkg}' not found on PyPI"}), 404
    if r.status_code != 200:
        return jsonify({"ok": False, "error": f"PyPI returned HTTP {r.status_code}"}), 502
    data = r.json()
    releases = data.get("releases", {})

    if version:
        if version not in releases:
            return jsonify({"ok": False, "error": f"Version {version} not found for {pkg}"}), 404
        candidates = releases[version]
    else:
        candidates = data.get("urls", [])
        if not candidates:
            if not releases:
                return jsonify({"ok": False, "error": "No releases found"}), 404
            top_ver = sorted(releases.keys())[-1]
            candidates = releases[top_ver]

    wheel = None
    for f in candidates:
        if f["filename"].endswith(".whl"):
            wheel = f
            break
    if not wheel:
        return jsonify({"ok": False, "error": "No .whl file found for this package"}), 404

    try:
        wr = requests.get(wheel["url"], timeout=60)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Wheel download failed: {type(e).__name__}"}), 502
    if wr.status_code != 200:
        return jsonify({"ok": False, "error": f"Wheel fetch returned HTTP {wr.status_code}"}), 502

    content = wr.content
    # Cap at 20 MB to keep JSON responses manageable
    if len(content) > 20 * 1024 * 1024:
        return jsonify({"ok": False, "error": "Wheel exceeds 20 MB cap"}), 413

    b64 = base64.b64encode(content).decode("ascii")
    return jsonify({
        "ok": True,
        "package": pkg,
        "version": wheel.get("version", "?"),
        "filename": wheel["filename"],
        "size_bytes": len(content),
        "b64": b64,
    })


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
