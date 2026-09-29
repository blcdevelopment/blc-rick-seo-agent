FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        fonts-dejavu-core \
        libffi8 \
        libgdk-pixbuf-2.0-0 \
        libharfbuzz-subset0 \
        libjpeg62-turbo \
        libopenjp2-7 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        shared-mime-info \
        # Virtual display + VNC for the one-time Semrush "connect" step on a headless server:
        # a human logs in through a REAL (headful) browser rendered on Xvfb and viewed over VNC,
        # so the session is minted from the server's own IP (see scripts/semrush_connect_server.sh).
        xvfb \
        x11vnc \
        fluxbox \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY . .
# Install pinned dependency versions from requirements.txt (the poetry.lock mirror) first so the
# image is reproducible, then install the local package itself without re-resolving its deps.
# Without this, the pyproject.toml floors would resolve to whatever is newest at build time, so
# prod could differ from the locked local dev environment.
RUN uv pip install --system -r requirements.txt \
    && uv pip install --system --no-deps -e .
# Pin Playwright's browser location so it is independent of XDG_CACHE_HOME (which the PDF
# renderer repoints at a font cache at runtime). Browsers install to and launch from here.
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
RUN python -m playwright install --with-deps chromium chromium-headless-shell

CMD ["celery", "-A", "apps.worker.celery_app.celery_app", "worker", "--loglevel=info"]
