# Spark Project (UV + PostgreSQL + django-environ)

A modern Django setup powered by **[uv](https://github.com/astral-sh/uv)** — a lightning-fast Python package and environment manager.  
This project uses **PostgreSQL** as the database and **django-environ** for secure environment configuration.

---

## Overview

This repository provides a clean, reproducible, and dependency-isolated Django environment designed for modern Python development.  
Key technologies used:
-  **[uv](https://github.com/astral-sh/uv)** — Fast package manager and virtual environment handler  
-  **PostgreSQL** — Relational database  
-  **django-environ** — Manage configuration and secrets  
-  **Django** — High-level Python web framework  

---

## Prerequisites

Before getting started, ensure the following are installed on your system:

- Python **3.10+**  
- PostgreSQL **18**
- Git  
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (install globally)

---

## Getting Started

### 1). Clone the Repository

```bash
git clone git@github.com:WERNSA/spark-api.git
cd spark-api
```

---

### 2). Configure Environment Variables

Create a `.env` file in the project root directory and add your configuration:

```env
DEBUG=True
SECRET_KEY=your-secret-key
ALLOWED_HOSTS=localhost
DATABASE_URL=postgres://postgres:postgres123@127.0.0.1:5432/db
GOOGLE_CLIENT_ID=your_client_id
GOOGLE_CLIENT_SECRET=your_client_secret
APPLE_CLIENT_ID=your_client_id
APPLE_CLIENT_SECRET=your_client_secret

# Google Calendar OAuth (for Google Calendar integration)
GOOGLE_OAUTH_CLIENT_ID=your_google_oauth_client_id
GOOGLE_OAUTH_CLIENT_SECRET=your_google_oauth_client_secret
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/google-calendar/callback

# Celery Configuration (for background tasks)
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# Test Database (for running tests)
TEST_DATABASE_URL=postgres:///spark_tests
```
---

### 3). Create the Environment and Install Dependencies

Use `uv` to create the virtual environment and install all required packages:

```bash
uv sync
```

---

### 4). Apply Database Migrations

Run migrate to sync your apps.

```bash
uv run python manage.py migrate
```

### 5). Start the Development Server

Launch the Django development server:

```bash
uv run python manage.py runserver
```

Visit the project at:  
**[http://localhost:8000/](http://localhost:8000/)**

---

### 6). Start Redis (Required for Celery)

Celery requires Redis as a message broker. Make sure Redis is running:

**macOS (using Homebrew):**
```bash
brew install redis
brew services start redis
```

**Linux (using apt):**
```bash
sudo apt-get install redis-server
sudo systemctl start redis
```

**Docker:**
```bash
docker run -d -p 6379:6379 redis:latest
```

Verify Redis is running:
```bash
redis-cli ping
# Should return: PONG
```

---

### 7). Start Celery Worker (For Background Tasks)

The Google Calendar integration uses Celery for asynchronous task processing. Start the Celery worker in a separate terminal:

```bash
uv run celery -A config worker -l info
```

For development with auto-reload on code changes:
```bash
uv run celery -A config worker -l info --reload
```

**Note:** The Celery worker must be running for Google Calendar sync tasks to execute. Events will be queued but not processed if the worker is not running.

---

### 8). Start Celery Beat (Optional - For Scheduled Tasks)

If you need to run periodic tasks, start Celery Beat in another terminal:

```bash
uv run celery -A config beat -l info
```

---

### 9). Running Tests

Tests use a separate database configured via `TEST_DATABASE_URL` in your `.env` file (defaults to `postgres:///spark_tests`).

**First time setup (one-time):**

If you've added new models, you may need to recreate the test database:

```bash
# Drop the test database (one-time only)
psql postgres -c "DROP DATABASE IF EXISTS spark_tests;"

# Run tests - database will be recreated automatically
uv run pytest --create-db
```

**Running tests:**

```bash
# Run all tests
uv run pytest

# Run specific test files
uv run pytest tenants/tests/test_google_calendar_mutations.py -v
```

---

## Useful Commands

| Command | Description |
|----------|-------------|
| `uv sync` | Create environment and install dependencies |
| `uv run python manage.py migrate` | Apply migrations |
| `uv run python manage.py runserver` | Start development server |
| `uv run celery -A config worker -l info` | Start Celery worker for background tasks |
| `uv run celery -A config worker -l info --reload` | Start Celery worker with auto-reload |
| `uv run celery -A config beat -l info` | Start Celery beat for scheduled tasks |
| `redis-cli ping` | Check if Redis is running |

---

### Generate GraphQL Schemas

Run the `export_schema` management command for each schema module to keep the `.graphql` snapshots up to date:

```bash
# Spark app schema
uv run python manage.py export_schema config.schema_spark:schema_spark --path schema_spark.graphql

# Client portal schema
uv run python manage.py export_schema config.schema_client:schema_clients --path schema_clients.graphql

# Ambassador portal schema
uv run python manage.py export_schema config.schema_ambassador:schema_ambassador --path schema_ambassador.graphql
```

---

## Contributing Guide (GitFlow)

### Branch Model

| Branch | Description |
|----------|-------------|
| `main` | Production-ready branch |
| `staging` | Pre-production testing branches |
| `develop` | Integration branch |
| `feature/*` | Created from develop for adding new features |
| `hotfix/*` | Created for urgent fixes |
