# Block 14 — Monitor Lifecycle and Incident Filtering

## What changed

Block 14 adds a persisted incident lifecycle to the monitor:

- `ACTIVE` and `RESOLVED` status
- Automatic resolution only after the incident's owning check completes successfully and no longer detects the fingerprint
- No resolution when that specific check fails
- Acknowledgment preserved while an incident remains continuously active, including severity changes
- Acknowledgment cleared when a resolved incident reopens
- `observation_count` incremented once per poll in which the fingerprint is detected
- `episode_count` incremented when a resolved incident returns
- Recommendation reuse for continuous incidents
- Recommendation regeneration only for new, reopened, or severity-changed incidents

The SQLite migration is additive. It does not drop or recreate the `issues` table. Existing Block 13 rows are retained, initialized as `ACTIVE`, receive `episode_count = 1`, and copy the legacy `occurrence_count` into `observation_count`.

## Dashboard and API filters

`GET /monitor/api/issues` supports:

- `status`: `ALL`, `ACTIVE`, `RESOLVED` (default: `ACTIVE`)
- `acknowledgment`: `ALL`, `ACKNOWLEDGED`, `UNACKNOWLEDGED` (default: `ALL`)
- `severity`: `INFO`, `WARNING`, `CRITICAL`
- `issue_type`: `TABLESPACE_USAGE`, `BLOCKING_SESSION`, `LONG_RUNNING_QUERY`, `ALERT_LOG_ERROR`, `WAIT_EVENT`
- `search`: case-insensitive text search across summary, details, recommendation, and fingerprint
- `sort`: `newest`, `oldest` (default: `newest`)
- `limit`: 1 through 500 (default: 100)

Example:

```text
/monitor/api/issues?status=ACTIVE&acknowledgment=UNACKNOWLEDGED&severity=CRITICAL&issue_type=BLOCKING_SESSION&search=payroll&sort=newest&limit=50
```

## Install and test commands

### Full ZIP

```bash
unzip oracle-dba-pydanticai-simple-block14.zip
cd oracle-dba-pydanticai-simple
uv sync --frozen
uv run python -W error::ResourceWarning -m unittest \
  tests.test_monitor_lifecycle \
  tests.test_monitor_poller \
  tests.test_monitor_filters \
  -v
```

### Patch onto the uploaded Block 13 source

```bash
cd oracle-dba-pydanticai-simple
patch -p1 < ../block14-monitor-lifecycle-and-filters.patch
uv sync --frozen
uv run python -W error::ResourceWarning -m unittest \
  tests.test_monitor_lifecycle \
  tests.test_monitor_poller \
  tests.test_monitor_filters \
  -v
```

Optional syntax compilation:

```bash
uv run python -m compileall -q monitor mcp_server tests
```

## Validation performed

- 10 isolated lifecycle, poller, migration, and REST filter tests passed.
- A copy of the uploaded `monitor.db` was migrated from 34 rows to 34 rows; the migration added the five lifecycle columns without deleting rows.
- No live Oracle database connection or Oracle integration test was performed.

## Modified files

- `mcp_server/server.py`
- `monitor/api.py`
- `monitor/checks.py`
- `monitor/models.py`
- `monitor/poller.py`
- `monitor/static/dashboard.html`
- `monitor/storage.py`
- `tests/test_monitor_summary_mcp.py`

## Created files

- `BLOCK14.md`
- `tests/test_monitor_filters.py`
- `tests/test_monitor_lifecycle.py`
- `tests/test_monitor_poller.py`
