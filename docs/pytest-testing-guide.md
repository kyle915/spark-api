# Pytest Testing Guide

This document provides comprehensive documentation for running and writing tests in the Spark API project using pytest.

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Test Database Setup](#test-database-setup)
- [Running Tests](#running-tests)
- [Test Structure](#test-structure)
- [Test Base Classes](#test-base-classes)
- [Writing Tests](#writing-tests)
- [Common Patterns](#common-patterns)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)

---

## Overview

The project uses **pytest** with **pytest-django** and **pytest-asyncio** for testing GraphQL endpoints and Django models. Tests are organized by app and use a PostgreSQL test database (`spark_tests`) to avoid affecting the development database.

### Key Features

- **PostgreSQL Test Database**: Uses `spark_tests` database (separate from development)
- **Async Support**: Full support for async GraphQL mutations and queries
- **Base Test Classes**: Reusable test utilities for creating test data
- **Transaction Safety**: Tests run in database transactions for isolation
- **Cache Testing**: Comprehensive tests for caching behavior

---

## Prerequisites

### Required Packages

The following packages are required (installed via `pyproject.toml`):

```toml
[dependency-groups]
dev = [
    "pytest>=8.0.0",
    "pytest-django>=4.8.0",
    "pytest-asyncio>=0.23.0",
]
```

### Database Setup

Ensure PostgreSQL is running and create the test database:

```bash
# Connect to PostgreSQL
psql postgres

# Create test database
CREATE DATABASE spark_tests;

# Exit psql
\q
```

The test database URL is configured as: `postgres:///spark_tests`

---

## Test Database Setup

The test database configuration is handled automatically by `tenants/tests/conftest.py`. This file:

1. Configures the test database to use PostgreSQL `spark_tests`
2. Preserves all Django database settings (AUTOCOMMIT, ATOMIC_REQUESTS, etc.)
3. Disables connection pooling for tests
4. Ensures `strawberry_django` is imported before schema imports

**Note**: The test database is automatically created and destroyed by pytest-django. The `--reuse-db` flag in `pytest.ini` allows reusing the database across test runs for faster execution.

---

## Running Tests

### Basic Commands

```bash
# Run all tests
uv run pytest

# Run tests with verbose output
uv run pytest -v

# Run tests for a specific app
uv run pytest tenants/tests
uv run pytest jobs/tests
uv run pytest events/tests

# Run a specific test file
uv run pytest tenants/tests/test_registration_mutations.py

# Run a specific test class
uv run pytest tenants/tests/test_registration_mutations.py::TestAmbassadorsRegistration

# Run a specific test method
uv run pytest tenants/tests/test_registration_mutations.py::TestAmbassadorsRegistration::test_register_ambassador_success

# Run tests matching a pattern
uv run pytest -k "test_register"

# Run tests and show print statements
uv run pytest -s

# Run tests and stop on first failure
uv run pytest -x

# Run tests with coverage
uv run pytest --cov=tenants --cov=jobs --cov=events
```

### Test Paths

Tests are organized in the following directories (configured in `pytest.ini`):

- `tenants/tests/` - User registration, tenant, and role tests
- `jobs/tests/` - Job-related mutation and query tests
- `events/tests/` - Event-related tests
- `ambassadors/tests/` - Ambassador-related tests
- `recaps/tests/` - Recap-related tests
- `chats/tests/` - Chat-related tests

---

## Test Structure

### Directory Structure

```
app_name/
├── tests/
│   ├── __init__.py          # Marks directory as Python package
│   ├── conftest.py          # Pytest configuration and fixtures
│   ├── base.py              # Base test classes with helper methods
│   └── test_*.py            # Test files
```

### Test File Naming

- Test files must match: `test_*.py` or `*_tests.py`
- Test classes must start with: `Test*`
- Test functions must start with: `test_*`

### Example Test File

```python
"""
Tests for Job queries in the jobs app.

This module tests:
- jobs query (Client, Spark, Ambassador schemas)
- job query (Client, Spark, Ambassador schemas)
"""
import pytest
import strawberry_django  # noqa: F401
from jobs.tests.base import JobsGraphQLTestCase


@pytest.mark.django_db(transaction=True)
class TestClientJobQueries(JobsGraphQLTestCase):
    """Tests for Job queries (Client schema)."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_client import schema_clients
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")
        # ... setup code ...
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    @pytest.mark.asyncio
    async def test_jobs_query_success(self):
        """Test successful jobs query."""
        query = """
        query JobsQuery($first: Int) {
            jobs(first: $first) {
                edges {
                    node {
                        id
                        name
                    }
                }
            }
        }
        """
        result = await self._execute_query_authenticated(
            query,
            {'first': 10},
            self.client_user
        )
        assert result.errors is None
        assert result.data is not None
```

---

## Test Base Classes

### Inheritance Hierarchy

```
BaseGraphQLTestCase (tenants/tests/base.py)
    ├── JobsGraphQLTestCase (jobs/tests/base.py)
    │   └── EventsGraphQLTestCase (events/tests/base.py)
    │       └── DashboardGraphQLTestCase (tenants/dashboard/tests/base.py)
```

### BaseGraphQLTestCase

Located in `tenants/tests/base.py`, provides:

- `get_system_user()` - Get or create system user
- `create_role(name, role_id=None, **kwargs)` - Create a Role
- `create_tenant(name="Test Tenant", **kwargs)` - Create a Tenant
- `create_user(username, email, role, password="password", **kwargs)` - Create a User
- `create_tenanted_user(user, tenant, **kwargs)` - Create TenantedUser relationship
- `setup_default_roles()` - Create default roles (Ambassador, Spark Admin, Client)
- `_execute_mutation(mutation, variables, endpoint_path=None)` - Execute GraphQL mutation

### JobsGraphQLTestCase

Located in `jobs/tests/base.py`, extends `BaseGraphQLTestCase` with:

- `create_status(name, tenant, **kwargs)` - Create a Status
- `create_company(name, email, phone, tenant, **kwargs)` - Create a Company
- `create_job(...)` - Create a Job with all dependencies
- `create_ambassador_job(...)` - Create an AmbassadorJob
- `_execute_mutation_authenticated(...)` - Execute mutation with authenticated user
- `_execute_query_authenticated(...)` - Execute query with authenticated user

### EventsGraphQLTestCase

Located in `events/tests/base.py`, extends `JobsGraphQLTestCase` with:

- `create_client(name, email, tenant, **kwargs)` - Create a Client
- `create_distributor(name, email, location, tenant, **kwargs)` - Create a Distributor
- `create_retailer(name, address, store_contact, location, tenant, **kwargs)` - Create a Retailer
- `create_request(...)` - Create a Request
- `create_event(...)` - Create an Event

### DashboardGraphQLTestCase

Located in `tenants/dashboard/tests/base.py`, extends `EventsGraphQLTestCase` with:

- `setup_dashboard_data(db)` - Comprehensive fixture that sets up all dashboard test data
- Pre-configured test data: events, requests, jobs, ambassadors, etc.

---

## Writing Tests

### Async Test Pattern

All GraphQL tests are async and use `@pytest.mark.asyncio`:

```python
@pytest.mark.asyncio
async def test_something(self):
    result = await self._execute_mutation_authenticated(
        mutation,
        variables,
        self.client_user
    )
    assert result.errors is None
```

### Database Transaction Marker

Always use `@pytest.mark.django_db(transaction=True)` for test classes:

```python
@pytest.mark.django_db(transaction=True)
class TestSomething(BaseGraphQLTestCase):
    # ...
```

This ensures:
- Database access is available
- Tests run in transactions for isolation
- Async operations can see data created in fixtures

### Setup Fixture Pattern

Use `@pytest.fixture(autouse=True)` for automatic setup:

```python
@pytest.fixture(autouse=True)
def setup(self, db):
    """Set up test data before each test."""
    from config.schema_client import schema_clients
    self.roles = self.setup_default_roles()
    self.tenant = self.create_tenant(name="Test Company")
    self.schema = schema_clients
    self.endpoint_path = "/api/v1/graphql/clients"
```

### Schema Import Pattern

Always import schemas inside the setup fixture to ensure `strawberry_django` is loaded:

```python
@pytest.fixture(autouse=True)
def setup(self, db):
    from config.schema_client import schema_clients  # Import here, not at top
    self.schema = schema_clients
```

### Synchronous ORM Calls in Async Tests

Wrap synchronous Django ORM calls with `sync_to_async`:

```python
from asgiref.sync import sync_to_async

@pytest.mark.asyncio
async def test_something(self):
    # ❌ Wrong - synchronous call in async context
    user = User.objects.get(username="test")
    
    # ✅ Correct - wrapped with sync_to_async
    user = await sync_to_async(User.objects.get)(username="test")
    
    # ✅ Also correct - using helper methods that are sync
    user = await sync_to_async(self.create_user)(
        username="test",
        email="test@test.com",
        role=self.roles['client']
    )
```

---

## Common Patterns

### Testing GraphQL Mutations

```python
@pytest.mark.asyncio
async def test_create_job_success(self):
    mutation = """
    mutation CreateJob($input: CreateJobInput!) {
        createJob(input: $input) {
            success
            message
            job {
                id
                name
            }
        }
    }
    """
    
    variables = {
        'input': {
            'name': 'Test Job',
            'code': 'JOB-001',
            # ... other fields
        }
    }
    
    result = await self._execute_mutation_authenticated(
        mutation,
        variables,
        self.client_user
    )
    
    assert result.errors is None
    assert result.data is not None
    assert result.data['createJob']['success'] is True
```

### Testing GraphQL Queries

```python
@pytest.mark.asyncio
async def test_jobs_query(self):
    query = """
    query {
        jobs(first: 10) {
            edges {
                node {
                    id
                    name
                }
            }
        }
    }
    """
    
    result = await self._execute_query_authenticated(
        query,
        {},
        self.client_user
    )
    
    assert result.errors is None
    assert result.data is not None
    assert len(result.data['jobs']['edges']) > 0
```

### Testing with Filters

```python
@pytest.mark.asyncio
async def test_events_stats_with_filters(self):
    query = """
    query EventsStats($startDate: String, $locationId: ID) {
        eventsStats(filters: {
            startDate: $startDate
            locationId: $locationId
        }) {
            totalEvents
        }
    }
    """
    
    result = await self._execute_query_authenticated(
        query,
        {
            'startDate': '2025-01-01',
            'locationId': str(self.location.id)
        },
        self.client_user
    )
    
    assert result.errors is None
    assert result.data is not None
```

### Testing Cache Behavior

```python
@pytest.mark.asyncio
async def test_cache_hit_same_filters(self):
    """Test that same filter combination returns cached result."""
    query = """
    query EventsStats($startDate: String) {
        eventsStats(filters: { startDate: $startDate }) {
            totalEvents
        }
    }
    """
    
    # Clear cache first
    from django.core.cache import cache
    cache.clear()
    
    filters = {'startDate': '2025-01-01'}
    
    # First call - should cache
    result1 = await self._execute_query_authenticated(
        query, filters, self.client_user
    )
    count1 = result1.data['eventsStats']['totalEvents']
    
    # Second call with same filters - should return cached result
    result2 = await self._execute_query_authenticated(
        query, filters, self.client_user
    )
    count2 = result2.data['eventsStats']['totalEvents']
    
    # Results should be identical (cached)
    assert count1 == count2
```

### Testing Error Cases

```python
@pytest.mark.asyncio
async def test_create_job_invalid_data(self):
    mutation = """
    mutation CreateJob($input: CreateJobInput!) {
        createJob(input: $input) {
            success
            message
        }
    }
    """
    
    variables = {
        'input': {
            'name': '',  # Invalid: empty name
            # ... other fields
        }
    }
    
    result = await self._execute_mutation_authenticated(
        mutation,
        variables,
        self.client_user
    )
    
    # Should have errors or success=False
    assert result.errors is not None or result.data['createJob']['success'] is False
```

---

## Configuration

### pytest.ini

Located in the project root:

```ini
[pytest]
DJANGO_SETTINGS_MODULE = config.settings
python_files = tests.py test_*.py *_tests.py
python_classes = Test*
python_functions = test_*
addopts = 
    --reuse-db          # Reuse test database across runs
    --nomigrations      # Skip migrations (faster)
    --tb=short          # Short traceback format
    -v                  # Verbose output
filterwarnings =
    ignore::DeprecationWarning:gqlauth.*
    ignore::DeprecationWarning:strawberry.*
    ignore::DeprecationWarning:functools.*
testpaths = 
    tenants/tests
    events/tests
    jobs/tests
    ambassadors/tests
    recaps/tests
    chats/tests
```

### Key Configuration Options

- **`--reuse-db`**: Reuses the test database across test runs (faster)
- **`--nomigrations`**: Skips running migrations (assumes schema is up to date)
- **`DJANGO_SETTINGS_MODULE`**: Points to Django settings
- **`testpaths`**: Directories where pytest looks for tests

---

## Troubleshooting

### Common Issues

#### 1. `ModuleNotFoundError: No module named 'transaction'`

**Solution**: Ensure `strawberry_django` is imported before schema imports. In test files, import schemas inside fixtures:

```python
@pytest.fixture(autouse=True)
def setup(self, db):
    from config.schema_client import schema_clients  # Import here
    self.schema = schema_clients
```

#### 2. `SynchronousOnlyOperation: You cannot call this from an async context`

**Solution**: Wrap synchronous Django ORM calls with `sync_to_async`:

```python
from asgiref.sync import sync_to_async

# ❌ Wrong
user = User.objects.get(username="test")

# ✅ Correct
user = await sync_to_async(User.objects.get)(username="test")
```

#### 3. `AttributeError: 'WSGIRequest' object has no attribute 'user'`

**Solution**: Use the base class helper methods (`_execute_mutation_authenticated`, `_execute_query_authenticated`) which properly mock the ASGI request.

#### 4. Test database not found

**Solution**: Create the test database:

```bash
psql postgres -c "CREATE DATABASE spark_tests;"
```

#### 5. Tests hanging indefinitely

**Solution**: Ensure you're using `@pytest.mark.django_db(transaction=True)` and that async operations are properly awaited.

#### 6. Cache not clearing between tests

**Solution**: Clear cache in your setup fixture:

```python
@pytest.fixture(autouse=True)
def setup(self, db):
    from django.core.cache import cache
    cache.clear()
    # ... rest of setup
```

### Debugging Tips

1. **Run with `-s` flag** to see print statements:
   ```bash
   uv run pytest -s
   ```

2. **Run with `-v` flag** for verbose output:
   ```bash
   uv run pytest -v
   ```

3. **Run single test** to isolate issues:
   ```bash
   uv run pytest path/to/test.py::TestClass::test_method -v
   ```

4. **Use `pytest.set_trace()`** for debugging:
   ```python
   import pytest
   
   def test_something(self):
       pytest.set_trace()  # Drop into debugger
       # ...
   ```

5. **Check test database**:
   ```bash
   psql spark_tests
   \dt  # List tables
   ```

---

## Best Practices

1. **Use base classes**: Extend appropriate base classes to reuse helper methods
2. **Clear cache**: Always clear cache in setup fixtures when testing caching
3. **Use transactions**: Always use `@pytest.mark.django_db(transaction=True)`
4. **Async/await**: Properly handle async operations in async tests
5. **Descriptive names**: Use clear test names that describe what is being tested
6. **Isolation**: Each test should be independent and not rely on other tests
7. **Cleanup**: Tests should clean up after themselves (handled by transactions)
8. **Documentation**: Add docstrings to test classes and methods explaining what they test

---

## Additional Resources

- [pytest Documentation](https://docs.pytest.org/)
- [pytest-django Documentation](https://pytest-django.readthedocs.io/)
- [pytest-asyncio Documentation](https://pytest-asyncio.readthedocs.io/)
- [Django Testing Documentation](https://docs.djangoproject.com/en/stable/topics/testing/)

---

## Summary

This testing setup provides:

- ✅ Isolated test database (PostgreSQL `spark_tests`)
- ✅ Full async support for GraphQL operations
- ✅ Reusable base classes and helper methods
- ✅ Comprehensive test coverage patterns
- ✅ Cache testing capabilities
- ✅ Transaction-safe test isolation

For questions or issues, refer to the troubleshooting section or check the existing test files for examples.

