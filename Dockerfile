# jobpipe in the cloud: the crawler and the dashboard in one container.
#   docker build -t jobpipe .
#   docker run -d -p 127.0.0.1:8050:8050 -v $PWD/data:/app/data \
#     -e JOBPIPE_DASHBOARD_TOKEN=... jobpipe
# State (databases, your details, resume, answers) lives in /app/data — mount a volume.
FROM python:3.12-slim

ARG WITH_LATEX=1
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    TZ=America/New_York \
    JOBPIPE_HEADLESS_ONLY=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=8050

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && if [ "$WITH_LATEX" = "1" ]; then \
      apt-get install -y --no-install-recommends texlive-latex-base texlive-latex-extra texlive-fonts-recommended; \
    fi \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY templates ./templates
COPY data/h1b_seed.json ./seed/h1b_seed.json
RUN pip install -e '.[apply,ai]' && playwright install --with-deps chromium

RUN useradd --create-home --uid 10001 jobpipe && mkdir -p /app/data && chown -R jobpipe /app/data
USER jobpipe
VOLUME /app/data
EXPOSE 8050

# Refuses to start without JOBPIPE_DASHBOARD_TOKEN: the dashboard holds your private details.
CMD ["sh", "-c", "jobpipe seed-h1b -f /app/seed/h1b_seed.json >/dev/null 2>&1; exec jobpipe serve --host 0.0.0.0 --port ${PORT}"]
