FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first (layer caching)
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Application source + bundled seed data
COPY src ./src
COPY seed_data ./seed_data
RUN pip install --no-deps -e .

EXPOSE 8000

CMD ["uvicorn", "mallory_engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
