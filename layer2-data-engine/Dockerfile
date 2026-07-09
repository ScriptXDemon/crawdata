FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy the whole project, then install. (The build backend needs src/ present, so we
# can't install from pyproject.toml alone — one COPY + one install is correct here.)
COPY pyproject.toml ./
COPY src ./src
COPY seed_data ./seed_data
RUN pip install --upgrade pip && pip install .

# The L2 /dashboard endpoint serves static/dashboard_live.html (resolved as /app/static).
COPY static ./static

EXPOSE 8000

CMD ["uvicorn", "mallory_engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
