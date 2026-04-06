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

# Redis Configuration (for django-rq background tasks)
CELERY_BROKER_URL=redis://localhost:6379/0

# Test Database (for running tests)
TEST_DATABASE_URL=postgres:///spark_tests

CLIENT_FRONTEND_URL=http://client.app
AMBASSADOR_FRONTEND_URL=http://ambassador.app
ADMIN_FRONTEND_URL=http://admin.app

RESEND_API_KEY=re_apikeyhere
MAIL_DRIVER=resend
DEFAULT_FROM_EMAIL=Spark <onboarding@resend.dev>
```
---

### 3). Create the Environment and Install Dependencies

Use `uv` to create the virtual environment and install all required packages:

```bash
uv sync
```

---

### 4). Install Native Dependencies (WeasyPrint)

WeasyPrint requires native libraries for PDF rendering.

**macOS (Homebrew):**
```bash
brew install pango cairo gdk-pixbuf libffi
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt install libpangocairo-1.0-0 libpango-1.0-0 libcairo2 libgdk-pixbuf2.0-0 libffi-dev
```

---

### 5). Apply Database Migrations

Run migrate to sync your apps.

```bash
uv run python manage.py migrate
```

### 6). Start the Development Server

Launch the Django development server:

```bash
uv run python manage.py runserver
```

Visit the project at:  
**[http://localhost:8000/](http://localhost:8000/)**

---

### 7). Start Redis (Required for django-rq)

django-rq requires Redis as a message queue. Make sure Redis is running:

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

### 8). Start RQ Worker (For Background Tasks)

The Google Calendar integration uses django-rq for asynchronous task processing. Start the RQ worker in a separate terminal:

```bash
uv run python manage.py rqworker high default low
```

Macos:
```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES uv run python manage.py rqworker default
```

This starts a worker that processes jobs from the `high`, `default`, and `low` queues (in that priority order).

**Note:** The RQ worker must be running for Google Calendar sync tasks to execute. Events will be queued but not processed if the worker is not running.

**For production deployment**, you can create a systemd service or use a process manager like supervisor. See the [django-rq documentation](https://github.com/rq/django-rq) for deployment examples.

Ambassador event reminders are scheduled automatically when an `AmbassadorJob`
is created or when the linked event's schedule changes.

For `AmbassadorJob` records that already existed before this reminder system was
deployed, run this one-time backfill command:

```bash
uv run python manage.py backfill_ambassador_event_reminders
```

---

### 9). Running Tests

Tests use a separate database configured via `TEST_DATABASE_URL` in your `.env` file (defaults to `postgres:///spark_tests`).

**First time setup (one-time):**

If you've added new models, you may need to recreate the test database:

```bash
# Drop the test database (one-time only)
createdb spark_tests

# Run tests - database will be recreated automatically
uv run pytest
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
| `uv run python manage.py rqworker high default low` | Start RQ worker for background tasks |
| `uv run python manage.py backfill_ambassador_event_reminders` | One-time backfill for exact ambassador reminder jobs |
| `redis-cli ping` | Check if Redis is running |
| `uv run python manage.py sync_events_to_google_calendar` | Sync existing events to Google Calendar for all connected users |
| `uv run python manage.py import_requests_batch --template-out /tmp/requests_template.xlsx` | Generate batch import template for requests |
| `uv run python manage.py import_requests_batch --file /tmp/requests.xlsx --tenant-id 1 --user-id 2` | Import requests in batch from Excel |

### Import Requests in Batch (Excel)

Use `openpyxl` to import multiple `Request` records at once:

```bash
# 1) Generate template
uv run python manage.py import_requests_batch --template-out /tmp/requests_template.xlsx

# 2) Validate without inserting
uv run python manage.py import_requests_batch \
  --file /tmp/requests.xlsx \
  --tenant-id 1 \
  --user-id 2 \
  --dry-run

# 3) Import
uv run python manage.py import_requests_batch \
  --file /tmp/requests.xlsx \
  --tenant-id 1 \
  --user-id 2
```

Optional defaults:
- `--default-timezone-id`
- `--default-request-type-id`
- `--sheet-name` (sheet index or name)

### Sync Events to Google Calendar

The `sync_events_to_google_calendar` management command allows you to sync existing events to Google Calendar. By default, it syncs all events that have a request (required for Google Calendar sync).

**Basic Usage:**
```bash
# Sync all events (default behavior)
uv run python manage.py sync_events_to_google_calendar

# Sync events for a specific tenant
uv run python manage.py sync_events_to_google_calendar --tenant-id 1

# Sync a specific event
uv run python manage.py sync_events_to_google_calendar --event-id 16

# Sync multiple events
uv run python manage.py sync_events_to_google_calendar --event-ids 16,17,18

# Sync events in a date range
uv run python manage.py sync_events_to_google_calendar --tenant-id 1 --from-date 2025-01-01 --to-date 2025-01-31

# Enqueue to RQ instead of running synchronously (recommended for large batches)
uv run python manage.py sync_events_to_google_calendar --enqueue

# Dry run to preview what would be synced
uv run python manage.py sync_events_to_google_calendar --dry-run
```

**Options:**
- `--tenant-id`: Filter events by tenant ID
- `--event-id`: Sync a specific event by ID
- `--event-ids`: Sync multiple events (comma-separated IDs)
- `--from-date`: Filter events from a date (YYYY-MM-DD)
- `--to-date`: Filter events up to a date (YYYY-MM-DD)
- `--no-request`: Include events without requests (not recommended)
- `--enqueue`: Enqueue sync jobs to RQ instead of running synchronously
- `--dry-run`: Preview what would be synced without actually syncing

**Note:** Events must have a request with a `start_time` to be synced to Google Calendar. The command will skip events that don't meet these requirements and show a summary at the end.

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

### Branch Model.

| Branch | Description |
|----------|-------------|
| `main` | Production-ready branch |
| `staging` | Pre-production testing branches |
| `develop` | Integration branch |
| `feature/*` | Created from develop for adding new features |
| `hotfix/*` | Created for urgent fixes |
