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

## Useful Commands

| Command | Description |
|----------|-------------|
| `uv sync` | Create environment and install dependencies |
| `uv run python manage.py migrate` | Apply migrations |
| `uv run python manage.py runserver` | Start development server |

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
