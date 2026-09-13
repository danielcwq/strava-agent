FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
ARG APP_REVISION=development
ENV APP_REVISION=$APP_REVISION

# Install dependencies first for build cache friendliness
COPY pyproject.toml ./
COPY uv.lock* ./
RUN uv sync --no-dev

# Application code (prompts/ ships so the system prompt is read at runtime)
COPY src/ ./src/
COPY prompts/ ./prompts/
COPY config/training_profile.example.toml ./config/training_profile.example.toml
COPY scripts/state_archive.py ./scripts/state_archive.py

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

EXPOSE 8080

# The Garmin secret is embedded in the URL because Garmin cannot send a custom
# auth header. Keep request paths out of application logs.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
