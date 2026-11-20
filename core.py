"""Core logic: proxy pool, candidate generation, platform checks, job runner.

Stdlib only. Every check returns one of:
  available | taken | invalid | rejected | unknown
"unknown" is never reported as available: errors, captchas and unrecognised
pages all land there.
"""
from __future__ import annotations

import http.client
import itertools
import json
import queue
import random
import re
import ssl
import string
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PLATFORMS = ("roblox", "discord", "tiktok", "youtube")
MAX_NAMES = 20000
ENUMERATE_LIMIT = 200_000
ALPHABETS = {
    "l": string.ascii_lowercase,                    # 4l = 4 letters
    "n": string.digits,                             # 4n = 4 digits
    "c": string.ascii_lowercase + string.digits,    # 4c = 4 letters/digits
}
RULES = {
    "roblox": {"min": 3, "max": 20, "pat": re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?")},
    "discord": {"min": 2, "max": 32, "pat": re.compile(r"[a-z0-9_.]+")},
    "tiktok": {"min": 2, "max": 24, "pat": re.compile(r"[A-Za-z0-9_.]+")},
    "youtube": {"min": 3, "max": 30, "pat": re.compile(r"[A-Za-z0-9_.\-]+")},
}


class Retry(Exception):
    """Transient problem: try again through a different proxy."""

    def __init__(self, msg: str, conn: bool = False):
        super().__init__(msg)
        self.conn = conn


# ---------------------------------------------------------------- proxies
@dataclass
class Proxy:
    url: str | None          # None = direct connection
    label: str
    cool: dict = field(default_factory=dict)   # platform or "*" -> monotonic time
    last: dict = field(default_factory=dict)   # platform -> monotonic time
    ok: int = 0
    bad: int = 0
    fails: int = 0
    dead: bool = False


def parse_proxies(text: str):
    """Accepts host:port, host:port:user:pass, user:pass@host:port, optional http:// prefix.
    Returns (proxies, skipped) where skipped is [(line, reason)]."""
    proxies, skipped, seen = [], [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith("socks"):
            skipped.append((line, "SOCKS is not supported (HTTP/HTTPS proxies only)"))
            continue
        for scheme in ("http://", "https://"):
            if low.startswith(scheme):
                line = line[len(scheme):]
                break
        user = pw = None
        try:
            if "@" in line and re.fullmatch(r"[^:@\s]+:\d+", line.rsplit("@", 1)[1]):
                cred, hostport = line.rsplit("@", 1)
                user, pw = cred.split(":", 1)
                host, port = hostport.rsplit(":", 1)
            else:
                parts = line.split(":")
                if len(parts) == 2:
                    host, port = parts
                elif len(parts) == 4:
                    host, port, user, pw = parts
                else:
                    raise ValueError("expected host:port")
        except ValueError:
            reason = "no port" if re.fullmatch(r"[\d.]+", line) else "unrecognised format"
            skipped.append((raw.strip(), reason))
            continue
        if not host or re.search(r"\s", host) or not port.isdigit() or not 1 <= int(port) <= 65535:
            skipped.append((raw.strip(), "bad host or port"))
            continue
        key = (host, port, user, pw)
        if key in seen:
            continue
        seen.add(key)
        auth = f"{quote(user, safe='')}:{quote(pw, safe='')}@" if user is not None else ""
        proxies.append(Proxy(url=f"http://{auth}{host}:{port}", label=f"{host}:{port}"))
    return proxies, skipped


class Pool:
    DEAD_AFTER = 3          # consecutive connection failures before a proxy is dropped
    RATE_COOLDOWN = 45.0    # seconds a proxy rests for one platform after 403/429

    def __init__(self, proxies=None):
        self.lock = threading.Lock()
        self.proxies: list[Proxy] = list(proxies or [])
        self.idx = 0

    @classmethod
    def direct(cls):
        return cls([Proxy(url=None, label="direct")])

    def replace(self, proxies):
        with self.lock:
            self.proxies = list(proxies)
            self.idx = 0

    def alive(self) -> int:
        with self.lock:
            return sum(1 for p in self.proxies if not p.dead)

    def acquire(self, platform: str, gap: float, stop: threading.Event):
        """Block until a live proxy is free for this platform.
        None if stopped or if no live proxy is left."""
        while not stop.is_set():
            wait = 1.0
            with self.lock:
                now = time.monotonic()
                n = len(self.proxies)
                if not any(not p.dead for p in self.proxies):
                    return None
                for k in range(n):
                    p = self.proxies[(self.idx + k) % n]
                    if p.dead:
                        continue
                    t = max(p.cool.get("*", 0), p.cool.get(platform, 0),
                            p.last.get(platform, 0) + gap)
                    if t <= now:
                        self.idx = (self.idx + k + 1) % n
                        p.last[platform] = now
                        return p
                    wait = min(wait, t - now)
            stop.wait(max(wait, 0.02))
        return None

    def good(self, p: Proxy):
        with self.lock:
            p.ok += 1
            p.fails = 0

    def penalize(self, p: Proxy, platform: str, conn: bool):
        with self.lock:
            p.bad += 1
            if conn:
                p.fails += 1
                if p.fails >= self.DEAD_AFTER:
                    p.dead = True
                else:
                    p.cool["*"] = time.monotonic() + 10
            else:
                p.cool[platform] = time.monotonic() + self.RATE_COOLDOWN

    def summary(self, top: int = 30):
        with self.lock:
            now = time.monotonic()
            alive = [p for p in self.proxies if not p.dead]
            cooling = sum(1 for p in alive if max(p.cool.values(), default=0) > now)
            rows = sorted(self.proxies, key=lambda p: (p.dead, -p.ok))[:top]
            return {"total": len(self.proxies), "alive": len(alive), "dead": len(self.proxies) - len(alive),
                    "cooling": cooling,
                    "top": [{"label": p.label, "ok": p.ok, "bad": p.bad, "dead": p.dead} for p in rows]}


# ---------------------------------------------------------------- candidates
def validate_name(platform: str, name: str):
    r = RULES[platform]
    if not r["min"] <= len(name) <= r["max"]:
        return f"length must be {r['min']}-{r['max']}"
    if not r["pat"].fullmatch(name):
        return "characters not allowed"
    if platform == "tiktok" and name.endswith("."):
        return "cannot end with a period"
    if platform == "discord" and ".." in name:
        return "no consecutive periods"
    return None


def generate(length: int, mode: str, count: int):
    chars = ALPHABETS[mode]
    space = len(chars) ** length
    count = max(1, min(count, space, MAX_NAMES))
    if space <= ENUMERATE_LIMIT:
        names = ["".join(p) for p in itertools.product(chars, repeat=length)]
        random.shuffle(names)
        return names[:count]
    seen = set()
    while len(seen) < count:
        seen.add("".join(random.choices(chars, k=length)))
    return list(seen)


# ---------------------------------------------------------------- HTTP + checkers
def fetch(url, proxy_url, method="GET", body=None, headers=None, timeout=12):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    if data:
        h["Content-Type"] = "application/json"
    mapping = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(mapping))
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read(400_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read(400_000).decode("utf-8", "replace")
        except Exception:
            text = ""
        return e.code, url, text
    except (urllib.error.URLError, OSError, ssl.SSLError, http.client.HTTPException, ValueError) as e:
        raise Retry(f"connection: {e}", conn=True)


def _limited(code):
    if code in (403, 429):
        raise Retry(f"HTTP {code}")


def check_roblox(name, proxy):
    url = ("https://auth.roblox.com/v1/usernames/validate"
           f"?request.username={quote(name)}&request.birthday=2000-01-01&request.context=Signup")
    code, _, text = fetch(url, proxy.url)
    _limited(code)
    if code != 200:
        return "unknown", f"HTTP {code}"
    try:
        c = json.loads(text).get("code")
    except ValueError:
        return "unknown", "response was not JSON"
    if c == 0:
        return "available", ""
    if c == 1:
        return "taken", ""
    if c == 2:
        return "rejected", "refused by Roblox (code 2)"
    return "unknown", f"unexpected code {c!r}"


def check_discord(name, proxy):
    code, _, text = fetch("https://discord.com/api/v9/unique-username/username-attempt-unauthed",
                          proxy.url, "POST", {"username": name},
                          {"Origin": "https://discord.com", "Referer": "https://discord.com/register"})
    _limited(code)
    if code != 200:
        return "unknown", f"HTTP {code}"
    try:
        taken = json.loads(text).get("taken")
    except ValueError:
        return "unknown", "response was not JSON"
    if taken is True:
        return "taken", ""
    if taken is False:
        return "available", ""
    return "unknown", "no 'taken' field"


def check_tiktok(name, proxy):
    code, _, text = fetch("https://www.tiktok.com/@" + quote(name), proxy.url)
    _limited(code)
    if '"statusCode":10221' in text:
        return "available", "user-not-found page"
    if re.search(r'"uniqueId":"%s"' % re.escape(name), text, re.I):
        return "taken", ""
    return "unknown", f"unrecognised page (HTTP {code}, captcha or layout change)"


def check_youtube(name, proxy):
    code, final, _ = fetch("https://www.youtube.com/@" + quote(name), proxy.url)
    _limited(code)
    if "consent." in final:
        raise Retry("consent redirect")
    if code == 404:
        return "available", "404, may still be reserved"
    if code == 200:
        return "taken", ""
    return "unknown", f"HTTP {code}"


CHECKERS = {"roblox": check_roblox, "discord": check_discord,
            "tiktok": check_tiktok, "youtube": check_youtube}


# ---------------------------------------------------------------- job
class Job:
    def __init__(self, pool, platforms, names, threads, gap_s, out_dir: Path,
                 on_result=None, skip=None):
        self.pool, self.platforms, self.gap = pool, list(platforms), gap_s
        self.out_dir = out_dir
        self.on_result = on_result
        self.tasks: queue.Queue = queue.Queue()
        skip = skip or set()
        for n in names:
            for p in self.platforms:
                if (p, n) not in skip:
                    self.tasks.put((p, n))
        self.total = self.tasks.qsize()
        self.threads = max(1, min(threads, self.total)) if self.total else 0
        self.results: list = []
        self.counts = Counter()
        self.done = 0
        self.active = 0
        self.starved = False       # True if every proxy died mid-job
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()

    def start(self):
        self.active = self.threads
        for _ in range(self.threads):
            threading.Thread(target=self._worker, daemon=True).start()

    def stop(self):
        self.stop_ev.set()

    @property
    def running(self):
        return self.active > 0

    def _check(self, platform, name):
        """(status, detail), or None when stopped / out of live proxies."""
        why = validate_name(platform, name)
        if why:
            return "invalid", why
        last = "no attempt"
        for _ in range(4):
            p = self.pool.acquire(platform, self.gap, self.stop_ev)
            if p is None:
                return None
            try:
                status, detail = CHECKERS[platform](name, p)
            except Retry as e:
                self.pool.penalize(p, platform, e.conn)
                last = str(e)
                continue
            except Exception as e:  # a parsing bug must not kill the worker
                return "unknown", f"internal error: {type(e).__name__}: {e}"
            self.pool.good(p)
            return status, detail
        return "unknown", f"gave up: {last}"

    def _worker(self):
        try:
            while not self.stop_ev.is_set():
                try:
                    platform, name = self.tasks.get_nowait()
                except queue.Empty:
                    return
                res = self._check(platform, name)
                if res is None:
                    if not self.stop_ev.is_set():
                        self.starved = True
                        self.stop_ev.set()
                    return
                self._record(platform, name, *res)
        finally:
            with self.lock:
                self.active -= 1

    def _record(self, platform, name, status, detail):
        with self.lock:
            self.results.append([platform, name, status, detail, time.strftime("%H:%M:%S")])
            self.counts[status] += 1
            self.done += 1
            if status == "available":
                self.out_dir.mkdir(parents=True, exist_ok=True)
                with open(self.out_dir / f"available_{platform}.txt", "a", encoding="utf-8") as f:
                    f.write(name + "\n")
        if self.on_result:
            try:
                self.on_result(platform, name, status, detail)
            except Exception:
                pass

    def snapshot(self, since: int):
        with self.lock:
            since = max(0, min(since, len(self.results)))
            return {"running": self.running, "total": self.total, "done": self.done,
                    "counts": dict(self.counts), "results": self.results[since:],
                    "next": len(self.results)}
