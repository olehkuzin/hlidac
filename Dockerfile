# Backend image (WP32): the FastAPI app, which also renders the pages. A separate frontend image
# comes once a frontend exists (WP33). Built and pushed to ACR by azure-pipelines.yml on release-*
# tags. Configuration only through environment variables; no .env, no patient data in the image.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so a code change doesn't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The app and its versioned content (content.py reads /app/data).
COPY src ./src
COPY data ./data
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home hlidac
USER hlidac

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# Behind the HTTPS proxy in the same container group (deploy/aci_spec.py), which sets the
# X-Forwarded-* headers; the app port itself is never public.
CMD ["/app/.venv/bin/uvicorn", "hlidac.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
