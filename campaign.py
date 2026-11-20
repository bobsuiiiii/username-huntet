"""AFK campaign: refresh proxies, check batches of names, log everything, repeat."""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path

import core
import proxy_check

MAX_CANDIDATES = 6000
DEFAULT_SOURCES = """\
# One URL per line (http/https). These are from memory and unverified: edit or replace them.
https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all
https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt
https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt
"""
DEFAULT_OWN = """\
# Your own (paid/private) proxies, one per line:
# host:port | host:port:user:pass | user:pass@host:port   (HTTP/HTTPS only)
"""


def normalize_cfg(req: dict) -> dict:
    def num(key, default, lo, hi):
        v = int(req.get(key, default))
        if not lo <= v <= hi:
            raise ValueError(f"{key} must be {lo}-{hi}")
        return v

    platforms = [p for p in req.get("platforms", []) if p in core.PLATFORMS]
    if not platforms:
        raise ValueError("pick at least one platform")
    custom = list(dict.fromkeys(n.strip().lower() for n in str(req.get("custom", "")).splitlines() if n.strip()))
    mode = req.get("mode", "c")
    if not custom and mode not in core.ALPHABETS:
        raise ValueError("mode must be l, n or c")
    return {
        "enabled": True, "platforms": platforms, "mode": mode, "custom": custom[:core.MAX_NAMES],
        "length": num("length", 4, 1, 12), "batch": num("batch", 300, 10, core.MAX_NAMES),
        "threads": num("threads", 20, 1, 200), "gap_ms": num("gap_ms", 1500, 0, 60000),
        "recheck_hours": num("recheck_hours", 24, 1, 720), "refresh_min": num("refresh_min", 20, 5, 720),
        "min_alive": num("min_alive", 5, 1, 1000), "max_free": num("max_free", 300, 10, 2000),
        "check_threads": num("check_threads", 150, 10, 400), "check_timeout": num("check_timeout", 4, 1, 15),
        "use_free": bool(req.get("use_free", True)), "use_own": bool(req.get("use_own", True)),
    }


class Campaign:
    def __init__(self, store, pool, data_dir: Path, notifier=None):
        self.store, self.pool, self.dir = store, pool, Path(data_dir)
        self.notifier = notifier
        self.thread = None
        self.stop_ev = threading.Event()
        self.force = threading.Event()
        self.job = None
        self.lock = threading.Lock()
        self.status = {"state": "stopped", "detail": "", "batches": 0, "last_refresh": None,
                       "candidates": 0, "live_free": 0, "source_errors": []}

    # ---- control
    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, cfg: dict):
        with self.lock:
            if self.running:
                raise ValueError("campaign already running")
            self.store.set_setting("campaign", cfg)
            self.stop_ev.clear()
            self.force.clear()
            self.thread = threading.Thread(target=self._loop, args=(cfg,), daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_ev.set()
        cfg = self.store.get_setting("campaign")
        if cfg:
            cfg["enabled"] = False
            self.store.set_setting("campaign", cfg)

    def resume(self):
        cfg = self.store.get_setting("campaign")
        if cfg and cfg.get("enabled"):
            self.start(cfg)
            return True
        return False

    def _set(self, state, detail=""):
        self.status["state"], self.status["detail"] = state, detail

    def _finish(self, cfg, state, detail):
        cfg = dict(cfg, enabled=False)
        self.store.set_setting("campaign", cfg)
        self._set(state, detail)

    def _on_result(self, platform, name, status, detail):
        self.store.add(platform, name, status, detail)
        if status == "available" and self.notifier:
            self.notifier.add(f"available on {platform}: `{name}`")

    # ---- proxies
    def _read(self, name, default):
        p = self.dir / name
        return p.read_text(encoding="utf-8") if p.exists() else default

    def _refresh(self, cfg):
        own, free, cands = [], [], []
        if cfg["use_own"]:
            own, _ = core.parse_proxies(self._read("proxies.txt", ""))
        if cfg["use_free"]:
            self._set("refreshing", "downloading proxy lists")
            urls = [l.strip() for l in self._read("sources.txt", DEFAULT_SOURCES).splitlines()
                    if l.strip() and not l.strip().startswith("#")]
            text, errors = proxy_check.download_sources(urls)
            self.status["source_errors"] = errors
            cands, _ = core.parse_proxies(text)
            own_labels = {p.label for p in own}
            cands = [p for p in cands if p.label not in own_labels]
            random.shuffle(cands)
            cands = cands[:MAX_CANDIDATES]
            self._set("refreshing", f"testing {len(cands)} proxies")
            live = proxy_check.filter_live(
                cands, cfg["check_threads"], cfg["check_timeout"], self.stop_ev,
                lambda d, n, l: self._set("refreshing", f"tested {d}/{n}, {l} working"))
            free = [p for p, _ in live[:cfg["max_free"]]]
        self.pool.replace(own + free)
        self.status.update(candidates=len(cands), live_free=len(free), last_refresh=time.time())

    # ---- main loop
    def _loop(self, cfg):
        direct = not (cfg["use_free"] or cfg["use_own"])
        pool = core.Pool.direct() if direct else self.pool
        last_refresh, force, empty = 0.0, True, 0
        custom = cfg["custom"]
        while not self.stop_ev.is_set():
            try:
                if not direct:
                    due = time.time() - last_refresh
                    if force or self.force.is_set() or (due > 60 and pool.alive() < cfg["min_alive"]) \
                            or due > cfg["refresh_min"] * 60:
                        self.force.clear()
                        self._refresh(cfg)
                        last_refresh, force = time.time(), False
                        if self.stop_ev.is_set():
                            break
                        if pool.alive() == 0:
                            self._set("waiting", "no working proxies, retrying in 60 s")
                            self.stop_ev.wait(60)
                            force = True
                            continue
                names = custom or core.generate(cfg["length"], cfg["mode"], cfg["batch"])
                skip = {(p, n) for p in cfg["platforms"]
                        for n in self.store.known(p, names, cfg["recheck_hours"])}
                job = core.Job(pool, cfg["platforms"], names, cfg["threads"], cfg["gap_ms"] / 1000,
                               self.dir / "results", on_result=self._on_result, skip=skip)
                if job.total == 0:
                    if custom:
                        self._finish(cfg, "finished", "all names were already checked recently")
                        return
                    empty += 1
                    if empty >= 20:
                        self._finish(cfg, "finished", "no unchecked names left in this search space")
                        return
                    continue
                empty = 0
                self.job = job
                job.start()
                while job.running and not self.stop_ev.is_set():
                    self._set("checking", f"batch {self.status['batches'] + 1}: {job.done}/{job.total}")
                    self.stop_ev.wait(0.5)
                if self.stop_ev.is_set():
                    job.stop()
                    break
                if job.starved:
                    force = True
                    if direct:
                        pool = core.Pool.direct()
                    self._set("waiting", "every proxy failed (network down?), retrying in 30 s")
                    self.stop_ev.wait(30)
                self.status["batches"] += 1
                if custom:
                    self._finish(cfg, "finished", "custom list done")
                    return
            except Exception as e:  # keep the AFK loop alive
                self._set("error", f"{type(e).__name__}: {e} (retrying in 30 s)")
                self.stop_ev.wait(30)
        self._set("stopped", "")
