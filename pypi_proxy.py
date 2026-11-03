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
                        ?python=3.12&platform=manylinux to match this pod
  /list/<package>       list available versions + wheel URLs (JSON)
                        ?python=3.12&platform=manylinux to filter wheels
                        ?latest=true to only show the newest matching release
  /b64/<package>        fetch a wheel and return it as base64 in a JSON body
                        (?version=X, ?python=X, ?platform=X). Use this when
                        the client can only read text, not binary.

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
import textwrap
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
# Wheel tag helpers
# ---------------------------------------------------------------------------

def _wheel_tags(filename: str):
    """Parse a .whl filename into (pytag, abitag, platform).

    e.g. 'markupsafe-3.0.2-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl'
      -> ('cp312', 'cp312', 'manylinux_2_17_x86_64.manylinux2014_x86_64')
    Pure wheels: py3-none-any -> ('py3', 'none', 'any')
    """
    stem = filename[:-4]  # strip .whl
    parts = stem.split("-")
    if len(parts) < 5:
        return None
    return parts[-3], parts[-2], parts[-1]


def _wheel_matches(wheel_file: dict, python: str = "", platform: str = "") -> bool:
    """Check if a wheel file matches the given python/platform filters."""
    fn = wheel_file["filename"]
    tags = _wheel_tags(fn)
    if tags is None:
        return False
    pytag, abitag, plat = tags

    # Python filter: "3.12" matches cp312 or py3 (pure)
    if python:
        py_norm = python.replace(".", "")  # "3.12" -> "312"
        if not (pytag == f"cp{py_norm}" or pytag.startswith("py")):
            return False

    # Platform filter: "manylinux" matches manylinux*, "musllinux" matches musllinux*,
    # "win" matches win_amd64 etc., "macosx" matches macosx*, "any" always matches
    if platform:
        p = platform.lower()
        if p != "any":
            if not (plat == "any" or p in plat):
                return False

    return True


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
        f"<li><a href='{BASE}/list/flask'>/list/&lt;package&gt;</a> — list versions (JSON)</li>"
        f"<li><a href='{BASE}/list/flask?python=3.12&platform=manylinux'>/list/&lt;package&gt;?python=X&amp;platform=Y</a> — filtered</li>"
        f"<li><a href='{BASE}/list/flask?latest=true'>/list/&lt;package&gt;?latest=true</a> — newest only</li>"
        f"<li><a href='{BASE}/download/flask'>/download/&lt;package&gt;</a> — download latest wheel</li>"
        f"<li><a href='{BASE}/download/flask?version=3.1.2'>/download/&lt;package&gt;?version=X</a> — pinned</li>"
        f"<li><a href='{BASE}/download/flask?python=3.12&platform=manylinux'>/download/&lt;package&gt;?python=X&amp;platform=Y</a> — platform-matched</li>"
        f"<li><a href='{BASE}/b64/flask'>/b64/&lt;package&gt;</a> — wheel as base64 JSON (text-safe)</li>"
        f"<li><a href='{BASE}/chunk/flask'>/chunk/&lt;package&gt;</a> — wheel in 8KB base64 slices</li>"
        f"<li><a href='{BASE}/chunk/flask?offset=8192'>/chunk/&lt;package&gt;?offset=N</a> — next slice</li>"
        f"<li><a href='{BASE}/warm/flask'>/warm/&lt;package&gt;</a> — pre-fetch to local disk cache</li>"
        f"<li><a href='{BASE}/status/flask'>/status/&lt;package&gt;</a> — is it cached? (instant)</li>"
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
            "endpoints": ["/", "/health", "/list/<pkg>", "/download/<pkg>", "/b64/<pkg>", "/chunk/<pkg>", "/warm/<pkg>", "/status/<pkg>"],
        }),
        mimetype="application/json",
    )


@app.route(f"{BASE}/list/<path:pkg>")
def list_versions(pkg: str):
    if not _valid_pkg(pkg):
        abort(400)
    python = request.args.get("python", "").strip()       # e.g. "3.12"
    platform = request.args.get("platform", "").strip()   # e.g. "manylinux", "win", "macosx"
    latest_only = request.args.get("latest", "").lower() in ("1", "true", "yes")
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
                if python or platform:
                    if not _wheel_matches(f, python, platform):
                        continue
                wheels.append({
                    "version": ver,
                    "filename": f["filename"],
                    "url": f["url"],
                    "python": f.get("requires_python"),
                })
    # If latest=true, only keep wheels from the highest version present
    if latest_only and wheels:
        top_ver = max(w["version"] for w in wheels)
        wheels = [w for w in wheels if w["version"] == top_ver]
    return Response(
        json.dumps({
            "name": data.get("name", pkg),
            "summary": data.get("summary", ""),
            "release_count": len(releases),
            "wheel_count": len(wheels),
            "filters": {"python": python or None, "platform": platform or None, "latest": latest_only},
            "wheels": wheels[:50],
        }),
        mimetype="application/json",
    )


def _pick_wheel(pkg: str, version: str, python: str = "", platform: str = ""):
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

    # Find a wheel among the candidates — prefer pure-Python (py3-none-any)
    # so the wheel works on any platform. If python/platform filters are given,
    # match those first; otherwise fall back to pure-Python, then any .whl.
    filtered = []
    for f in candidates:
        fn = f["filename"]
        if not fn.endswith(".whl"):
            continue
        if python or platform:
            if _wheel_matches(f, python, platform):
                filtered.append(f)
        else:
            filtered.append(f)

    if not filtered:
        return None, _err(
            f"No .whl file found for {pkg} matching "
            f"python={python or 'any'} platform={platform or 'any'} "
            f"(candidates: {[f['filename'] for f in candidates[:5]]}). "
            "This package may only ship source distributions."
        )

    # Prefer pure-Python wheels (work everywhere), then cp-specific, then any
    pure = None
    any_whl = None
    for f in filtered:
        fn = f["filename"]
        if "py3-none-any" in fn or "py2.py3-none-any" in fn:
            if pure is None:
                pure = f
        elif any_whl is None:
            any_whl = f
    if pure is not None:
        return pure, None
    if any_whl is not None:
        return any_whl, None
    return filtered[0], None


@app.route(f"{BASE}/download/<path:pkg>")
def download_wheel(pkg: str):
    if not _valid_pkg(pkg):
        abort(400)
    version = request.args.get("version", "").strip()
    python = request.args.get("python", "").strip()
    platform = request.args.get("platform", "").strip()
    wheel, err = _pick_wheel(pkg, version, python, platform)
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
    python = request.args.get("python", "").strip()
    platform = request.args.get("platform", "").strip()
    wheel, err = _pick_wheel(pkg, version, python, platform)
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

    b64_str = base64.b64encode(content).decode("ascii")
    b64_wrapped = "\n".join(textwrap.wrap(b64_str, 76)) if b64_str else ""
    payload = {
        "ok": True,
        "package": pkg,
        "version": wheel.get("version", "?"),
        "filename": wheel["filename"],
        "size_bytes": len(content),
        "from_cache": from_cache,
        "b64_lines": b64_wrapped.split("\n") if b64_wrapped else [],
        "b64": b64_str,
    }
    body = (
        "<!DOCTYPE html><html><head><title>"
        + pkg + " b64</title></head><body>"
        + "<pre>" + json.dumps(payload, indent=2) + "</pre></body></html>"
    )
    return Response(body, mimetype="text/html")


# ---------------------------------------------------------------------------
# Chunked transfer — pull a wheel in small text-safe slices
# ---------------------------------------------------------------------------

CHUNK_DEFAULT = 8 * 1024   # 8 KB of raw bytes per chunk (~11 KB base64)
CHUNK_MAX = 32 * 1024      # cap at 32 KB raw per request


@app.route(f"{BASE}/chunk/<path:pkg>")
def chunk_wheel(pkg: str):
    """Return a slice of the wheel as base64, for text-only fetchers.

    Usage:
        /chunk/flask                        → metadata + first chunk
        /chunk/flask?offset=8192            → next chunk
        /chunk/flask?python=3.12&platform=manylinux&offset=0

    Response JSON:
        {ok, package, version, filename, size_bytes,
         total_b64_len, offset, chunk_size, remaining, b64}

    Client loops: offset += chunk_size until remaining == 0,
    concatenates all b64 strings, decodes → wheel file.
    """
    if not _valid_pkg(pkg):
        abort(400)
    version = request.args.get("version", "").strip()
    python = request.args.get("python", "").strip()
    platform = request.args.get("platform", "").strip()
    wheel, err = _pick_wheel(pkg, version, python, platform)
    if err is not None:
        return err

    try:
        content, from_cache = _fetch_wheel_bytes(wheel)
    except Exception as e:
        return _err(f"Wheel download failed: {type(e).__name__}: {e}")

    if len(content) > B64_MAX_BYTES:
        return _err(
            f"Wheel too large ({len(content)} bytes > "
            f"{B64_MAX_BYTES} cap). Use /download/ instead.", 413
        )

    full_b64 = base64.b64encode(content).decode("ascii")
    total_b64 = len(full_b64)

    try:
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        return _err("offset must be an integer", 400)
    if offset < 0 or offset >= total_b64:
        return _err(f"offset out of range (0..{total_b64 - 1})", 400)

    try:
        size = int(request.args.get("size", str(CHUNK_DEFAULT)))
    except ValueError:
        return _err("size must be an integer", 400)
    size = max(512, min(size, CHUNK_MAX))

    # Slice the base64 string directly (safe: base64 has no structure
    # that breaks on arbitrary cuts as long as we reassemble before decode)
    end = min(offset + size, total_b64)
    chunk = full_b64[offset:end]

    # Wrap the chunk in 76-char lines so text-extractors don't choke on
    # one giant unbroken token. Client strips whitespace before decoding.
    chunk_wrapped = "\n".join(textwrap.wrap(chunk, 76)) if chunk else ""

    # Wrap in minimal HTML so text-extractors (Perplexity/Firecrawl) treat
    # it as a normal page rather than rejecting raw/base64-looking bodies.
    payload = {
        "ok": True,
        "package": pkg,
        "version": wheel.get("version", "?"),
        "filename": wheel["filename"],
        "size_bytes": len(content),
        "from_cache": from_cache,
        "total_b64_len": total_b64,
        "offset": offset,
        "chunk_size": end - offset,
        "remaining": total_b64 - end,
        "b64_lines": chunk_wrapped.split("\n") if chunk_wrapped else [],
        "b64": chunk,
    }
    body = (
        "<!DOCTYPE html><html><head><title>"
        + pkg + " chunk " + str(offset) + "</title></head><body>"
        + "<pre>" + json.dumps(payload, indent=2) + "</pre></body></html>"
    )
    return Response(body, mimetype="text/html")


# ---------------------------------------------------------------------------
# Warm / status — pre-fetch a wheel to local disk, then verify before chunking
# ---------------------------------------------------------------------------

@app.route(f"{BASE}/warm/<path:pkg>")
def warm_wheel(pkg: str):
    """Download the wheel to local disk cache. Returns fast confirmation.

    Call this FIRST (it can take 10-30s on a cold start). Then check /status/
    and pull /chunk/ slices which will be instant cache hits.
    """
    if not _valid_pkg(pkg):
        abort(400)
    version = request.args.get("version", "").strip()
    python = request.args.get("python", "").strip()
    platform = request.args.get("platform", "").strip()
    wheel, err = _pick_wheel(pkg, version, python, platform)
    if err is not None:
        return err

    try:
        content, from_cache = _fetch_wheel_bytes(wheel)
    except Exception as e:
        return _err(f"Wheel download failed: {type(e).__name__}: {e}")

    payload = {
        "ok": True,
        "package": pkg,
        "version": wheel.get("version", "?"),
        "filename": wheel["filename"],
        "size_bytes": len(content),
        "from_cache": from_cache,
        "message": "cached locally, ready for /chunk/" if not from_cache else "already cached",
    }
    body = (
        "<!DOCTYPE html><html><head><title>"
        + pkg + " warm</title></head><body>"
        + "<pre>" + json.dumps(payload, indent=2) + "</pre></body></html>"
    )
    return Response(body, mimetype="text/html")


@app.route(f"{BASE}/status/<path:pkg>")
def status_wheel(pkg: str):
    """Check if a wheel is in the local disk cache (instant, no PyPI call).

    Returns: {ok, package, filename, size_bytes, cached: true/false}
    """
    if not _valid_pkg(pkg):
        abort(400)
    version = request.args.get("version", "").strip()
    python = request.args.get("python", "").strip()
    platform = request.args.get("platform", "").strip()
    wheel, err = _pick_wheel(pkg, version, python, platform)
    if err is not None:
        return err

    filename = wheel["filename"]
    cache_path = os.path.join(WHEEL_CACHE_DIR, filename)
    cached = os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0
    size = os.path.getsize(cache_path) if cached else 0

    payload = {
        "ok": True,
        "package": pkg,
        "version": wheel.get("version", "?"),
        "filename": filename,
        "cached": cached,
        "size_bytes": size,
    }
    body = (
        "<!DOCTYPE html><html><head><title>"
        + pkg + " status</title></head><body>"
        + "<pre>" + json.dumps(payload, indent=2) + "</pre></body></html>"
    )
    return Response(body, mimetype="text/html")


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
