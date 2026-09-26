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
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY . .
# Install pinned dependency versions from requirements.txt (the poetry.lock mirror) first so the
# image is reproducible, then install the local package itself without re-resolving its deps.
# Without this, the pyproject.toml floors (e.g. fastapi>=0.111.0) would resolve to whatever is
# newest at build time, so prod could differ from the locked local dev environment.
RUN uv pip install --system -r requirements.txt \
    && uv pip install --system --no-deps -e .

EXPOSE 8000

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
