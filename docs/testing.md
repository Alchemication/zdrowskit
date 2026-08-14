# Testing

```bash
uv run pytest                                      # run all tests
uv run pytest -v                                   # verbose output
uv run pytest --cov=src --cov-report=term-missing  # with coverage
uv run pytest tests/test_parsers_metrics.py        # single file
uv run ruff check .
uv run ruff format .
uv run --group site python marketing/build.py      # build the public docs/site
```

Tests live in `tests/` with fixture data in `tests/fixtures/`.

The suite covers parsers for metrics, workouts, and GPX; aggregation logic; the SQLite store round-trip; report formatting; LLM utility functions; and the `run_sql` tool, including SQL validation, read-only safety, row limits, and query execution.

Shared fixtures such as sample snapshots and in-memory DBs are in `tests/conftest.py`.

Normal `uv run pytest` uses mocks and must never call a real LLM. Real-model
regressions use the separate, opt-in [LLM eval runner](evals.md), which spends
provider quota.

The site build renders every `docs/*.md` page and fails on unresolved landing
page placeholders or missing pre-rendered Mermaid assets. After changing a
Mermaid block, regenerate its committed SVG first:

```bash
uv run python marketing/render_diagrams.py
```
