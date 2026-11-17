FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app

# All state (SQLite database, the portal's SSH key) lives under /data so the
# container itself stays stateless: one volume locally, one PVC on Kubernetes.
RUN useradd --uid 10001 --no-create-home portal \
    && mkdir /data && chown portal /data
USER 10001

ENV PATH=/app/.venv/bin:$PATH \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
