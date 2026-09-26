# Shared Oracle query layer

Oracle SQL used by the interactive MCP tools and by monitor checks is owned by
`oracle_core/queries.py`.

`monitor/checks.py` should contain monitoring policy only: thresholds, severity,
fingerprints, rate calculations, and incident creation. It calls the synchronous
shared query functions through `asyncio.to_thread` so database I/O does not block
the monitor event loop.

Current shared monitor/MCP query functions include:

- `get_blocking_sessions()`
- `get_wait_events()`
- `get_tablespace_usage()`
- `get_long_running_sessions()`
- `get_alert_log_errors()`
- `get_write_workload_counters()`

The existing interactive tools `get_active_sessions()` and `get_top_sql()` also
live in the same layer. As more tools are added, keep reviewed Oracle SQL here
rather than embedding SQL in MCP wrappers or monitor policy code.
