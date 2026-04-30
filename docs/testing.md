# Testing

```bash
uv run pytest                                      # run all tests
uv run pytest -v                                   # verbose output
uv run pytest --cov=src --cov-report=term-missing  # with coverage
uv run pytest tests/test_parsers_metrics.py        # single file
uv run ruff check .
uv run ruff format .
```

Tests live in `tests/` with fixture data in `tests/fixtures/`.

The suite covers parsers for metrics, workouts, and GPX; aggregation logic; the SQLite store round-trip; report formatting; LLM utility functions; and the `run_sql` tool, including SQL validation, read-only safety, row limits, and query execution.

Shared fixtures such as sample snapshots and in-memory DBs are in `tests/conftest.py`.

Normal `uv run pytest` uses mocks and must never call a real LLM.
