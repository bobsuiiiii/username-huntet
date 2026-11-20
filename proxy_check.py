"""Fast proxy sorter and free-list downloader."""
from __future__ import annotations

import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import core

# Google's connectivity-check URL: answers 204 with no body, and it is HTTPS, so a pass
# proves the proxy supports CONNECT and does not break TLS (certificates are verified).
TEST_URL = "https://www.gstatic.com/generate_204"


def probe(proxy_url: str, timeout: float):
    """Latency in seconds if the proxy works, else None."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    req = urllib.request.Request(TEST_URL, headers={"User-Agent": core.UA})
    t0 = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as r:
            ok = r.status == 204
    except Exception:
        return None
    dt = time.monotonic() - t0
    return dt if ok and dt <= timeout * 2 else None


def filter_live(proxies, threads, timeout, stop, progress=None):
    """Test many proxies in parallel. Returns [(proxy, latency)] fastest first."""
    live, done = [], 0
    ex = ThreadPoolExecutor(max_workers=max(1, threads))
    try:
        futs = {ex.submit(probe, p.url, timeout): p for p in proxies}
        for f in as_completed(futs):
            if stop.is_set():
                break
            done += 1
            lat = f.result()
            if lat is not None:
                live.append((futs[f], lat))
            if progress and done % 50 == 0:
                progress(done, len(futs), len(live))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    live.sort(key=lambda x: x[1])
    return live


def download_sources(urls, timeout=15):
    """Fetch proxy lists directly (not through a proxy). Returns (text, errors)."""
    parts, errors = [], []
    for u in urls:
        if not u.lower().startswith(("http://", "https://")):
            errors.append(f"{u}: only http(s) URLs")
            continue
        try:
            req = urllib.request.Request(u, headers={"User-Agent": core.UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                parts.append(r.read(5_000_000).decode("utf-8", "replace"))
        except Exception as e:
            errors.append(f"{u}: {e}")
    return "\n".join(parts), errors
