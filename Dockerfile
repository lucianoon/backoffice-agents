FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN pip install --no-cache-dir uv && (uv sync --no-dev --frozen --extra postgres || uv sync --no-dev --extra postgres)
COPY src ./src
COPY data/samples ./data/samples
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src
ENTRYPOINT ["backoffice"]
CMD ["worker", "--watch"]
