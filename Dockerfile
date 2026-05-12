FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first for build cache friendliness
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-dev

# Application code (prompts/ ships so the system prompt is read at runtime)
COPY src/ ./src/
COPY prompts/ ./prompts/

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
