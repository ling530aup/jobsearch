# Repository Guidelines

## Project Structure & Module Organization

- `Argus/` contains crawler orchestration, models, filtering, persistence, and ATS adapters in `Argus/ats/`.
- `config/profiles/` stores crawler profiles; `job_results/` is local generated output and is git-ignored.
- `web/server.py` serves the local dashboard and exposes MySQL-backed APIs.
- `frontend/` contains the dashboard: `js/api.js` handles requests, `state.js` owns state, `ui.js` renders DOM, and `app.js` coordinates events and polling.
- `sql/jobsearch_schema.sql` is the fresh MySQL schema. Top-level scripts such as `run_search.py` and `search.py` are CLI entry points.

## Build, Test, and Development Commands

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python run_search.py                 # Run the default profile
python run_search.py custom           # Run a named profile
./venv/bin/python web/server.py       # Start dashboard at :8787
ARGUS_PORT=9000 ./venv/bin/python web/server.py
pytest                                # Run tests configured by pyproject.toml
```

The dashboard requires a reachable MySQL database and reads connection settings from `config/database.yaml` or `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASE` environment variables. Provision the schema from `sql/jobsearch_schema.sql`; do not reintroduce runtime migration logic.

## Coding Style & Naming Conventions

Use 4-space indentation, PEP 8-oriented Python, `snake_case` for functions and variables, `PascalCase` for classes, and descriptive constants in `UPPER_SNAKE_CASE`. Keep ATS-specific behavior in its adapter module. Frontend JavaScript uses camelCase; keep API, state, rendering, and event logic in their existing modules. Add focused comments for non-obvious crawler or database behavior. No formatter or linter is currently configured.

## Testing Guidelines

Place Python tests under `tests/` with names like `test_filter.py` or `test_mysql_store.py`; pytest discovers `test_*.py`. Prefer unit tests for filters, pagination, deduplication, and API payloads. Keep live crawler and database tests isolated or explicitly marked because they require external services.

## Commit & Pull Request Guidelines

Use short, imperative commit subjects, optionally scoped (for example, `Fix location matching`). Pull requests should explain behavior changes, database/schema impact, configuration requirements, and verification commands. Include dashboard screenshots for UI changes and note any required MySQL or Playwright setup.

## Architecture & Data Safety

The crawler loads existing canonical URLs from local result files before saving; preserve this deduplication boundary. New jobs are written to JSON/CSV first and then best-effort to MySQL, associated with the current `crawl_runs` record. The dashboard reads MySQL and updates only `jobs.applied`; it must not overwrite that field during crawler upserts. Never commit credentials, personal profiles, generated results, or database dumps.
