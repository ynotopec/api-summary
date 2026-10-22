FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TIKTOKEN_CACHE_DIR=/app/tokenizer
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY api_summary ./api_summary
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" && useradd --uid 10001 --create-home app && mkdir /data && chown app:app /data
USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
CMD ["uvicorn", "api_summary.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
