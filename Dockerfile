# Build stage: install Pulse and its locked dependencies into a virtual environment.
FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Runtime stage: the virtual environment only, run as a non-root user.
FROM python:3.14-slim
RUN useradd --system --uid 10001 --no-create-home pulse
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER pulse
ENTRYPOINT ["pulse"]
CMD ["schedule", "--config", "/config/config.yaml"]
