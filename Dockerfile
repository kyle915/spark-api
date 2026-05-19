ARG PYTHON_VERSION=3.13-slim
FROM python:${PYTHON_VERSION}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_SYSTEM_PYTHON=1

RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /code

WORKDIR /code

RUN pip install uv

COPY pyproject.toml uv.lock /code/

RUN uv sync --frozen --no-dev

COPY . /code

RUN chmod +x /code/scripts/entrypoint.sh

EXPOSE 8000

# Entry-point honors $PORT (Cloud Run injects this — typically 8080)
# and runs `manage.py migrate --noinput` before exec'ing hypercorn,
# so each new image lands its migrations on first container boot.
CMD ["/code/scripts/entrypoint.sh"]