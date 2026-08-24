# Deploying to free hosting (no VPS, no card)

## Why not the "obvious" free options

This app drives a **headless Chromium** in the backend (that's how
`sofascore-wrapper` gets past SofaScore's 403-on-plain-REST), which rules out
most of the usual free-hosting suspects:

| Platform | Why it won't work here |
|---|---|
| **PythonAnywhere (free)** | Free accounts can only make outbound requests to a small domain whitelist — sofascore.com isn't on it, so every fetch would fail outright. |
| **Vercel / Netlify (free)** | Serverless functions with short timeouts (~10s) and no way to persist an installed Chromium binary between invocations — this app's first request alone can take 20-40s. |
| **Fly.io (free)** | Free allowance is a 256MB VM — too small to reliably run headless Chromium without OOM crashes. Also now requires a card on file. |
| **Railway** | No longer has an indefinite free tier (trial credit only, then paid). |
| **Google Cloud Run (free tier)** | Actually a solid fit technically (Docker-based, scales to zero, generous free quota) — but requires an active billing account with a card on file, even to use the free quota. Worth it if you're okay adding a card; skipped here since you said "free hosting" without one. |

## The one that actually works: Render (free web service)

Render's free tier: no credit card required, runs arbitrary Docker images,
proper outbound internet access, 512MB RAM. The tradeoff — the free tier
**sleeps after 15 minutes of no traffic** and takes ~30-60s to wake up, on
top of this app's own 20-40s first-load (Chromium spin-up). So the very
first request after it's been idle can take up to ~60-90s total. Every
request after that is normal speed until it sleeps again. Fine for a
personal/low-traffic project; not fine if you need instant responses at all
times.

I've added a `Dockerfile`, `.dockerignore`, and `render.yaml` to the project
for this.

---

## Steps

### 1. Push the code to GitHub

Render deploys from a Git repo (public or private).

```bash
cd /path/to/the/project
git init
git add app.py requirements.txt Dockerfile .dockerignore render.yaml README.md
git commit -m "Sports analyzer"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/sports-analyzer.git
git push -u origin main
```

(Create the empty repo on GitHub first if you haven't.)

### 2. Create the Render service

1. Go to https://render.com and sign up (GitHub login is easiest — no card
   needed for the free tier).
2. **New +** → **Web Service** → connect your GitHub repo.
3. Render should auto-detect the `Dockerfile` and offer **Docker** as the
   environment. If it also detects `render.yaml`, it'll pre-fill the plan as
   **Free** and the health check path as `/api/sports` — otherwise set those
   manually.
4. Click **Create Web Service**. First build takes a few minutes (installing
   Chromium + its system deps).

### 3. Verify

Once it deploys, Render gives you a URL like
`https://sports-analyzer.onrender.com`. Open it — expect the first load to
be slow (cold start + Chromium spin-up combined), then fast on subsequent
requests until it sleeps again.

### 4. Point crownme.fun at it (optional)

In Render: your service → **Settings** → **Custom Domains** → add
`crownme.fun` and `www.crownme.fun`. Render gives you a CNAME/A record to
add at your DNS provider. Since you mentioned Cloudflare earlier: keep the
DNS record **DNS-only (grey cloud)** rather than proxied, or Render's own
free-tier TLS cert issuance can conflict with Cloudflare's proxy — you can
re-enable the orange cloud afterward once the cert's issued if you want
Cloudflare's proxy/CDN in front.

---

## Living with the free-tier limits

- **Cold starts**: nothing to fix on the free tier — it's the tradeoff for
  not paying. If it matters, Render's paid tier ($7/mo) removes the sleep.
- **512MB RAM**: this is why the Dockerfile pins `--workers 1 --threads 2` —
  don't raise worker count on the free plan, each concurrent request briefly
  runs its own Chromium and you'll get OOM-killed.
- **Build minutes**: Render's free tier includes enough build minutes for
  occasional redeploys of a project this size; you won't need to think about
  it unless you're pushing many times a day.

## Updating later

Just push to `main` — Render redeploys automatically on every push (unless
you've turned that off in settings).
