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

EXPOSE 8000

CMD ["uv", "run", "hypercorn", "config.asgi:application", "--bind", "0.0.0.0:8000"]