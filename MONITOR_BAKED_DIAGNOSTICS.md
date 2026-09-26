# Monitor recommendation diagnostics

Monitor recommendation SQL is application-owned and reviewed. The LLM does not generate SQL or executable commands.

## Current flow

1. `monitor/checks.py` detects an incident.
2. `monitor/poller.py` decides whether a fresh recommendation is needed (new, reopened, or severity changed).
3. `monitor/diagnostics.py` runs approved diagnostics for registered issue types.
4. `monitor/llm.py` sends the measured incident details plus approved diagnostic evidence to the recommendation model.
5. The model returns only root cause, recommended action, risk if ignored, and confidence (0.0-1.0).

## TABLESPACE_USAGE diagnostics

`oracle_core.queries.get_tablespace_diagnostics()` validates the PDB and retrieves file-level evidence from CDB views, including:

- datafile name and file ID
- current size
- autoextend status
- effective maximum size
- next autoextend increment
- current tablespace free space

Additional incident types can be added later to `DIAGNOSTICS_BY_ISSUE` in `monitor/diagnostics.py` using the same pattern.
