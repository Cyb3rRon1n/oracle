# Oracle game server — the authoritative engine + LLM narrator, spoken to
# over raw websockets. Build context is the repo root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8765 \
    SESSION_STORE_DIR=/data/sessions

WORKDIR /app

# pyproject.toml is the single source of truth for dependencies. The
# [ollama] extra is always installed so DM_BACKEND=ollama needs no rebuild.
COPY pyproject.toml ./
COPY shared/ shared/
COPY server/ server/
RUN pip install ".[ollama]"

RUN useradd --create-home --uid 10001 oracle \
    && mkdir -p "$SESSION_STORE_DIR" \
    && chown -R oracle /data
USER oracle

EXPOSE 8765
VOLUME ["/data/sessions"]

# The server speaks raw websockets — there is no HTTP endpoint to curl, so
# the check is a plain TCP connect to the listen port.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ['SERVER_PORT'])), 2).close()"

CMD ["python", "-m", "server.main"]
