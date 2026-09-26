# Monitor approved SQL

The monitor dashboard displays deterministic Oracle SQL owned by application code.
The language model does not create or edit these statements.

Current coverage:

- `TABLESPACE_USAGE`
  - verify datafile size, autoextend, and max size
  - verify free space
  - reviewed add-datafile remediation template
- `BLOCKING_SESSION`
  - verify blocker/waiter relationship
  - inspect blocking session and SQL
  - reviewed kill-session remediation template
- `LONG_RUNNING_QUERY`
  - inspect the active session
  - inspect its SQL text and execution statistics
  - reviewed kill-session remediation template
- `ALERT_LOG_ERROR`
  - review recent alert-log entries matching the detected ORA code
  - verify current instance status
- `WAIT_EVENT`
  - inspect the cumulative system-event statistics
  - identify current user sessions waiting on the event
- `WRITE_WORKLOAD`
  - inspect write-related `V$SYSSTAT` counters
  - identify recently active DML
  - inspect `log file sync`
  - inspect redo log switch frequency

Verification actions are read-only. Remediation templates are marked as requiring DBA
review and are never executed automatically.

The SQL uses Oracle Database 12.2/19c-compatible dynamic performance and dictionary
views already used by this project. Runtime privileges still determine whether the
monitoring account can execute a particular statement.
