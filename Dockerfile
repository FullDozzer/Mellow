FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY mellow ./mellow
RUN pip install --no-cache-dir .
COPY config ./config
COPY alembic.ini ./
COPY migrations ./migrations
CMD ["sh", "-c", "alembic upgrade head && mellow"]
