"""Batches 'available' finds into Discord webhook messages (optional)."""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request

import core

PREFIXES = ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")


def post(url: str, content: str):
    req = urllib.request.Request(url, data=json.dumps({"content": content}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": core.UA})
    urllib.request.urlopen(req, timeout=10).read()


class Notifier:
    def __init__(self, url: str = "", window: float = 5.0):
        self.url = url.strip()
        if self.url and not self.url.startswith(PREFIXES):
            raise ValueError("DISCORD_WEBHOOK must be a discord.com webhook URL")
        self.window = window
        self.q: queue.Queue = queue.Queue()
        if self.url:
            threading.Thread(target=self._run, daemon=True).start()

    def add(self, line: str):
        if self.url:
            self.q.put(line)

    def _run(self):
        while True:
            lines = [self.q.get()]
            end = time.monotonic() + self.window
            while time.monotonic() < end and sum(len(l) + 1 for l in lines) < 1800:
                try:
                    lines.append(self.q.get(timeout=max(0.05, end - time.monotonic())))
                except queue.Empty:
                    break
            try:
                post(self.url, "\n".join(lines)[:1900])
            except Exception:
                pass            # a failed alert must never affect the campaign
