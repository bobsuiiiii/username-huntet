import threading

import core


def test_parse_proxies_formats_and_skips():
    text = "\n".join([
        "# comment", "1.2.3.4:8080", "5.6.7.8:3128:bob:p@ss", "carol:pw@9.9.9.9:80",
        "http://1.2.3.4:8080", "45.38.107.97", "socks5://1.1.1.1:1080", "1.1.1.1:99999",
    ])
    proxies, skipped = core.parse_proxies(text)
    urls = [p.url for p in proxies]
    assert urls == ["http://1.2.3.4:8080", "http://bob:p%40ss@5.6.7.8:3128", "http://carol:pw@9.9.9.9:80"]
    reasons = dict(skipped)
    assert reasons["45.38.107.97"] == "no port"
    assert "SOCKS" in reasons["socks5://1.1.1.1:1080"]
    assert "1.1.1.1:99999" in reasons


def test_generate_modes():
    n = core.generate(4, "l", 50)
    assert len(n) == len(set(n)) == 50 and all(len(x) == 4 and x.isalpha() for x in n)
    c = core.generate(4, "c", 30)          # 36^4 > enumerate limit: random path
    assert len(set(c)) == 30 and all(x.isalnum() and x == x.lower() for x in c)
    assert len(core.generate(2, "n", 500)) == 100   # capped by space


def test_validate_name():
    assert core.validate_name("roblox", "ab") is not None
    assert core.validate_name("roblox", "abcd") is None
    assert core.validate_name("tiktok", "ab.") is not None
    assert core.validate_name("discord", "AB12") is not None   # uppercase not allowed


class P:  # stand-in proxy
    url = None


def fake(monkeypatch, code=200, url="u", text=""):
    monkeypatch.setattr(core, "fetch", lambda *a, **k: (code, url, text))


def test_roblox(monkeypatch):
    for body, want in (('{"code":0}', "available"), ('{"code":1}', "taken"), ('{"code":2}', "rejected"), ('{"code":9}', "unknown")):
        fake(monkeypatch, text=body)
        assert core.check_roblox("abcd", P())[0] == want


def test_discord(monkeypatch):
    fake(monkeypatch, text='{"taken": true}'); assert core.check_discord("abcd", P())[0] == "taken"
    fake(monkeypatch, text='{"taken": false}'); assert core.check_discord("abcd", P())[0] == "available"
    fake(monkeypatch, text='{}'); assert core.check_discord("abcd", P())[0] == "unknown"


def test_youtube_and_tiktok(monkeypatch):
    fake(monkeypatch, code=404); assert core.check_youtube("abcd", P())[0] == "available"
    fake(monkeypatch, code=200); assert core.check_youtube("abcd", P())[0] == "taken"
    fake(monkeypatch, code=200, url="https://consent.youtube.com/x")
    try:
        core.check_youtube("abcd", P()); assert False
    except core.Retry:
        pass
    fake(monkeypatch, text='..."statusCode":10221...'); assert core.check_tiktok("abcd", P())[0] == "available"
    fake(monkeypatch, text='..."uniqueId":"ABCD"...'); assert core.check_tiktok("abcd", P())[0] == "taken"
    fake(monkeypatch, text='<html>captcha</html>'); assert core.check_tiktok("abcd", P())[0] == "unknown"


def test_rate_limit_raises_retry(monkeypatch):
    fake(monkeypatch, code=429)
    try:
        core.check_roblox("abcd", P()); assert False
    except core.Retry as e:
        assert not e.conn


def test_job_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setitem(core.CHECKERS, "roblox", lambda n, p: ("available" if n.startswith("a") else "taken", ""))
    job = core.Job(core.Pool.direct(), ["roblox"], ["abcd", "zzzz", "ab"], 2, 0, tmp_path)
    job.start()
    for _ in range(200):
        if not job.running:
            break
        threading.Event().wait(0.02)
    snap = job.snapshot(0)
    assert snap["done"] == 3 and snap["counts"] == {"available": 1, "taken": 1, "invalid": 1}
    assert (tmp_path / "available_roblox.txt").read_text() == "abcd\n"


# ---------------- v2: store, dead proxies, campaign, proxy sorter
import time as _time

import campaign as camp
import proxy_check
from store import Store


def test_store_known_query_counts(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add("roblox", "abcd", "taken"); s.add("roblox", "wxyz", "unknown", "HTTP 429"); s.add("discord", "abcd", "available")
    assert s.known("roblox", ["abcd", "wxyz", "qqqq"], 24) == {"abcd"}      # unknown is never "known"
    rows, total = s.query(status="taken")
    assert total == 1 and rows[0][2] == "abcd"
    c = s.counts()
    assert c["total"]["taken"] == 1 and c["available_by_platform"] == {"discord": 1}
    assert len(list(s.export())) == 3
    s.set_setting("k", {"a": 1}); assert s.get_setting("k") == {"a": 1} and s.get_setting("nope", 5) == 5


def test_proxy_dies_and_job_starves(monkeypatch, tmp_path):
    def conn_fail(n, p):
        raise core.Retry("connection: refused", conn=True)
    monkeypatch.setitem(core.CHECKERS, "roblox", conn_fail)
    pool = core.Pool([core.Proxy("http://a:1", "a:1")])
    monkeypatch.setattr(core.Pool, "DEAD_AFTER", 2)
    job = core.Job(pool, ["roblox"], ["abcd", "efgh"], 1, 0, tmp_path)
    job.start()
    for _ in range(300):
        if not job.running:
            break
        _time.sleep(0.05)
    assert job.starved and pool.alive() == 0 and job.done == 0   # nothing wrongly logged as a result


def test_retry_rotates_proxy_fast(monkeypatch, tmp_path):
    monkeypatch.setattr(core.Pool, "RATE_COOLDOWN", 0.01)
    def boom(n, p):
        raise core.Retry("HTTP 429")
    monkeypatch.setitem(core.CHECKERS, "discord", boom)
    pool = core.Pool([core.Proxy("http://a:1", "a:1"), core.Proxy("http://b:2", "b:2")])
    job = core.Job(pool, ["discord"], ["abcd"], 1, 0, tmp_path)
    assert job._check("discord", "abcd")[0] == "unknown"


def test_normalize_cfg():
    c = camp.normalize_cfg({"platforms": ["roblox", "bogus"], "length": 4, "mode": "c"})
    assert c["platforms"] == ["roblox"] and c["enabled"]
    for bad in ({"platforms": []}, {"platforms": ["roblox"], "mode": "x"}, {"platforms": ["roblox"], "threads": 0}):
        try:
            camp.normalize_cfg(bad); assert False
        except ValueError:
            pass


def test_filter_live_sorts_fastest_first(monkeypatch):
    lat = {"http://a:1": 0.9, "http://b:2": None, "http://c:3": 0.2}
    monkeypatch.setattr(proxy_check, "probe", lambda url, t: lat[url])
    ps = [core.Proxy(u, u[7:]) for u in lat]
    out = proxy_check.filter_live(ps, 3, 4, threading.Event())
    assert [p.label for p, _ in out] == ["c:3", "a:1"]


def test_campaign_custom_list_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setitem(core.CHECKERS, "roblox", lambda n, p: ("available" if n == "abcd" else "taken", ""))
    store = Store(tmp_path / "t.db")
    c = camp.Campaign(store, core.Pool(), tmp_path)
    cfg = camp.normalize_cfg({"platforms": ["roblox"], "custom": "abcd\nzzzz", "use_free": False, "use_own": False, "gap_ms": 0})
    c.start(cfg)
    for _ in range(200):
        if not c.running:
            break
        _time.sleep(0.05)
    assert c.status["state"] == "finished"
    assert store.counts()["total"] == {"available": 1, "taken": 1}
    assert store.get_setting("campaign")["enabled"] is False
    assert (tmp_path / "results" / "available_roblox.txt").read_text() == "abcd\n"
    # second run: everything is already known, so nothing is re-checked
    c.start(camp.normalize_cfg({"platforms": ["roblox"], "custom": "abcd\nzzzz", "use_free": False, "use_own": False}))
    for _ in range(200):
        if not c.running:
            break
        _time.sleep(0.05)
    assert "already checked" in c.status["detail"] and store.counts()["total"] == {"available": 1, "taken": 1}


import notify


def test_notifier_batches_and_validates(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "post", lambda url, text: sent.append(text))
    n = notify.Notifier("https://discord.com/api/webhooks/1/abc", window=0.2)
    n.add("one"); n.add("two")
    for _ in range(100):
        if sent:
            break
        _time.sleep(0.05)
    assert sent == ["one\ntwo"]
    assert notify.Notifier("").url == ""
    try:
        notify.Notifier("http://evil.example/x"); assert False
    except ValueError:
        pass
