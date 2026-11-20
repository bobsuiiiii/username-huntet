# Username checker v2 (AFK + hosted)

Checks 4c / 4l / 4n (or your own list) usernames on Roblox, Discord, TikTok and YouTube through proxies, logs every result in SQLite, and keeps going on its own.

## Run locally / Codespace
    python server.py                 # Python 3.9+, no dependencies -> http://127.0.0.1:8080

## Host it 24/7 (important: it must run on a machine that stays on)
The AFK loop is a background thread. Hosts that sleep your app when nobody visits (many free web tiers) stop it,
and hosts without a persistent disk lose the logs on every restart. Check the current free-tier terms before choosing.

Best fit I know of: a small always-on VM (for example Oracle Cloud "Always Free", which needs card verification). On the VM:

    sudo apt update && sudo apt install -y python3
    unzip username-checker.zip && cd username-checker
    # edit deploy/usercheck.service: set APP_PASSWORD, paths and User
    sudo cp deploy/usercheck.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now usercheck
    # open TCP port 8080 in the VM's firewall/security list, then visit http://<vm-ip>:8080

Docker instead: `docker build -t usercheck . && docker run -d --restart always -p 8080:8080 -e APP_PASSWORD=secret -v usercheck-data:/data usercheck`

The server refuses to listen publicly without APP_PASSWORD (browser login box: any username, that password).
Basic auth over plain http sends the password unencrypted, so put it behind HTTPS (e.g. a free Cloudflare Tunnel or Caddy) for real use.

Do NOT commit your proxies or the data folder to a public GitHub repo. Paste your proxies into the site after deploying.

## AFK behaviour
- Start the campaign once. It saves its settings, and after a restart or reboot it resumes by itself.
- Loop: refresh proxies -> test them fast -> check a batch of random names -> log everything -> repeat.
- Names with a conclusive result (available/taken/invalid/rejected) are skipped for "Re-check after" hours. `unknown` is always retried.
- Proxies that fail 3 connections in a row are dropped. If all die it refreshes the lists. If nothing works it waits 60 s and retries.
- Logs: Logs card (filter by status/platform) and Download CSV. Available names are also in DATA_DIR/results/available_<platform>.txt.

## Proxy sorter
Each candidate is tested with an HTTPS request through the proxy (CONNECT, certificate verified) to a Google 204 URL,
4 s timeout by default, 150 in parallel, fastest kept. Free proxies are mostly dead or slow, expect a low pass rate.
Free sources are in DATA_DIR/sources.txt (editable in the UI). The default URLs are from memory: check the "Free proxy list URLs" error box.

## Result meanings
available / taken / unknown (error, rate limit, captcha, unrecognised page; never counted as available) / invalid (breaks naming rules) / rejected (platform refused the name).
"available" is a check result, not a reservation.


## No card? Render free + uptime pinger + Discord alerts (from memory, check current terms)
1. Put this folder in a PRIVATE GitHub repo (no proxies inside).
2. Render -> New -> Web Service -> pick the repo -> Runtime: Docker -> Instance type: Free.
3. Environment variables: APP_PASSWORD (required), DISCORD_WEBHOOK (your webhook URL, optional but recommended).
4. After deploy, make a free monitor (e.g. UptimeRobot) hitting https://<your-app>.onrender.com/ping every 5 minutes. /ping needs no login and returns "ok".
5. Open the site, log in (any username + APP_PASSWORD), paste your proxies, press Start.
Free web services on Render sleep without traffic and have no persistent disk, so a restart wipes the log database
and the campaign must be started again. DISCORD_WEBHOOK is why finds are not lost: each available name is posted there.
