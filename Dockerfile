FROM python:3.12-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir uv \
    && useradd --system --uid 10001 --gid 0 --home-dir /app --no-create-home app

# dependências primeiro (camada em cache); --locked falha se o uv.lock não bater com o pyproject
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --extra postgres

COPY src ./src
COPY data/samples ./data/samples
COPY data/kb ./data/kb
COPY tenants ./tenants
RUN uv sync --locked --no-dev --extra postgres \
    && chown -R app:0 /app/data && chmod -R g=u /app/data

ENV PATH="/app/.venv/bin:$PATH"
USER app

# confere configuração e acesso ao banco (DB_URL), sem rede externa nem segredos na saída
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["backoffice", "healthcheck"]

ENTRYPOINT ["backoffice"]
CMD ["worker", "--watch"]
