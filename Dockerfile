FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir uv==0.12.0 && uv sync --locked --no-dev --extra chroma
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "faq_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
