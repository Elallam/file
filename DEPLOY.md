# Deploying to your VPS

Assumes an Ubuntu/Debian VPS (22.04/24.04) with root or sudo access. Adjust
package manager commands if you're on something else (the general flow is
the same).

Because `sofascore-wrapper` drives a **headless Chromium**, the VPS setup is
a bit more than a typical Flask deploy — Chromium needs a handful of system
libraries that aren't installed on a bare server by default. Playwright's own
installer handles this for you (`--with-deps` below).

---

## 1. System packages & Python env

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx

# app directory
sudo mkdir -p /opt/tt-app
sudo chown $USER:$USER /opt/tt-app
cd /opt/tt-app
```

Upload `app.py` and `requirements.txt` here (scp, git clone, rsync — whatever
you prefer):

```bash
scp app.py requirements.txt youruser@your-vps-ip:/opt/tt-app/
```

Then on the VPS:

```bash
cd /opt/tt-app
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# installs Chromium AND its OS-level dependencies (fonts, libnss3, etc.)
python -m playwright install --with-deps chromium
```

That last command is the one people usually miss — without `--with-deps`,
Chromium installs but immediately crashes on a fresh server because it's
missing shared libraries.

Quick sanity check before wiring up nginx/systemd:

```bash
HOST=127.0.0.1 PORT=8000 python app.py
# in another shell:
curl -s http://127.0.0.1:8000/api/sports
# Ctrl+C the app once you see the JSON response
```

---

## 2. Run it as a service (systemd)

This repo includes `deploy/tt-app.service`. Copy it in and adjust the `User`
and paths if you didn't use `/opt/tt-app`:

```bash
sudo cp deploy/tt-app.service /etc/systemd/system/tt-app.service
sudo systemctl daemon-reload
sudo systemctl enable --now tt-app
sudo systemctl status tt-app     # should show "active (running)"
```

Notes on the service file:
- `--workers 2` — each concurrent request spins up its own headless Chromium
  (~250–400MB RAM). Keep this **low** (1–2) on a small VPS (1–2GB RAM) and
  only raise it if you've confirmed the headroom. Watch `htop`/`free -h`
  under load the first few times.
- `--timeout 120` — first request per league/tournament can take 20–40s
  (Chromium startup + multiple page navigations for odds). Gunicorn's
  default 30s timeout will kill it mid-request otherwise.

Logs:
```bash
sudo journalctl -u tt-app -f
```

---

## 3. Put nginx in front of it

Copy the template and edit `server_name`:

```bash
sudo cp deploy/nginx-tt-app.conf /etc/nginx/sites-available/tt-app
sudo nano /etc/nginx/sites-available/tt-app   # set your domain or use `_` for IP-only
sudo ln -s /etc/nginx/sites-available/tt-app /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

At this point `http://your-vps-ip/` (or your domain) should show the app.

---

## 4. HTTPS (recommended if you have a domain)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your.domain.com
```

Certbot edits the nginx config in place and sets up auto-renewal — no
further action needed. Skip this step if you're only using a bare IP (no
domain), since Let's Encrypt requires a domain name.

---

## 5. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'    # opens 80 + 443
sudo ufw enable
```

Don't open port 8000 externally — nginx is the only thing that should reach
gunicorn; it stays bound to `127.0.0.1` in the service file.

---

## Updating the app later

```bash
cd /opt/tt-app
# replace app.py with the new version
sudo systemctl restart tt-app
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 502 Bad Gateway from nginx | `tt-app` service isn't running — check `journalctl -u tt-app -f` |
| Request hangs then 504 | Increase `proxy_read_timeout` / gunicorn `--timeout` further, or your VPS is too small to run Chromium quickly under load |
| Chromium crashes immediately | Re-run `python -m playwright install --with-deps chromium` — a missing OS library is the usual cause |
| High memory usage / OOM kills | Lower gunicorn `--workers` to 1, or upgrade the VPS — each concurrent request briefly runs a Chromium instance |
