# Container image for free-tier PaaS hosting (Render, Fly.io, Cloud Run, etc.)
# Same install steps as the VPS guide: pip deps + Playwright's own Chromium
# + system-library installer, just packaged so a platform can build it for you.

FROM python:3.11-slim

WORKDIR /app

# minimal base packages Playwright's installer needs to fetch/verify Chromium
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY app.py .

# Most free PaaS platforms inject $PORT and expect the app to bind to it.
ENV PORT=8080
EXPOSE 8080

# workers=1, threads=2: free tiers give ~512MB RAM; each concurrent request
# runs its own headless Chromium (~250-400MB), so keep concurrency low.
# timeout=120: first request per league spins up Chromium + several page
# loads (odds, and for football also match stats) - can take 20-40s+.
CMD ["sh", "-c", "gunicorn --workers 1 --threads 2 --timeout 120 --bind 0.0.0.0:$PORT app:app"]
