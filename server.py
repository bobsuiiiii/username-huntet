#!/usr/bin/env python3
"""Username checker web app.

  python server.py                       local:  http://127.0.0.1:8080
  APP_PASSWORD=secret HOST=0.0.0.0 PORT=8080 DATA_DIR=/data python server.py   hosted

A non-loopback HOST without APP_PASSWORD is refused.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hmac
import io
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import campaign as camp
import core
from notify import Notifier
from store import Store

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
PASSWORD = os.environ.get("APP_PASSWORD", "")
STATUSES = {"available", "taken", "unknown", "invalid", "rejected"}


class App:
    store: Store
    pool = core.Pool()
    campaign: camp.Campaign


def read_file(name, default):
    p = DATA / name
    return p.read_text(encoding="utf-8") if p.exists() else default


class Handler(BaseHTTPRequestHandler):
    server_version = "usercheck/2.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _authed(self):
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode("utf-8", "replace").partition(":")[2]
                if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                    return True
            except ValueError:
                pass
        self._send(401, b'{"error":"login required"}', extra={"WWW-Authenticate": 'Basic realm="usercheck"'})
        return False

    def do_GET(self):
        if urlparse(self.path).path == "/ping":      # for uptime monitors; exposes nothing
            return self._send(200, b"ok", "text/plain")
        if not self._authed():
            return
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        status = q.get("status") if q.get("status") in STATUSES else None
        platform = q.get("platform") if q.get("platform") in core.PLATFORMS else None
        if u.path == "/":
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            c = App.campaign
            st = dict(c.status)
            job = c.job
            st["job"] = {"done": job.done, "total": job.total} if job and job.running else None
            self._json({"campaign": st, "running": c.running, "pool": App.pool.summary(),
                        "counts": App.store.counts(), "now": time.time()})
        elif u.path == "/api/config":
            self._json({"campaign": App.store.get_setting("campaign"),
                        "proxies": read_file("proxies.txt", camp.DEFAULT_OWN),
                        "sources": read_file("sources.txt", camp.DEFAULT_SOURCES)})
        elif u.path == "/api/logs":
            try:
                rows, total = App.store.query(status, platform, int(q.get("limit", 100)), int(q.get("offset", 0)))
            except ValueError:
                return self._json({"error": "bad paging"}, 400)
            self._json({"rows": rows, "total": total})
        elif u.path == "/api/export.csv":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="usernames.csv"')
            self.end_headers()                       # HTTP/1.0: body ends when the connection closes
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["utc_time", "platform", "name", "status", "detail"])
            self.wfile.write(buf.getvalue().encode())
            for ts, pl, name, st, detail in App.store.export(status, platform):
                buf.seek(0); buf.truncate()
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)), pl, name, st, detail])
                self.wfile.write(buf.getvalue().encode())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authed():
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self._json({"error": "bad origin"}, 403)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 <= n <= 2_000_000:
                return self._json({"error": "body too large"}, 413)
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise ValueError("expected a JSON object")
            if self.path == "/api/campaign/start":
                App.campaign.start(camp.normalize_cfg(req))
                return self._json({"ok": True})
            if self.path == "/api/campaign/stop":
                App.campaign.stop()
                return self._json({"ok": True})
            if self.path == "/api/refresh":
                if not App.campaign.running:
                    raise ValueError("start the campaign first; it refreshes proxies as part of running")
                App.campaign.force.set()
                return self._json({"ok": True})
            if self.path in ("/api/proxies", "/api/sources"):
                name = "proxies.txt" if self.path == "/api/proxies" else "sources.txt"
                text = str(req.get("text", ""))
                (DATA / name).write_text(text, encoding="utf-8")
                out = {"ok": True}
                if name == "proxies.txt":
                    good, skipped = core.parse_proxies(text)
                    out.update(usable=len(good), skipped=[f"{l} ({r})" for l, r in skipped])
                return self._json(out)
            self._json({"error": "not found"}, 404)
        except (ValueError, TypeError) as e:
            self._json({"error": str(e)}, 400)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1") and not PASSWORD:
        sys.exit("refusing to listen on a public address without APP_PASSWORD set")
    DATA.mkdir(parents=True, exist_ok=True)
    App.store = Store(DATA / "app.db")
    App.campaign = camp.Campaign(App.store, App.pool, DATA, Notifier(os.environ.get("DISCORD_WEBHOOK", "")))
    if App.campaign.resume():
        print("resumed the campaign that was running before the restart")
    print(f"data dir: {DATA}\nopen http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
